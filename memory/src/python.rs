//! The store as a Python module, for the tools that read a finished run.
//!
//! judge, rebuild and obsidian stay Python: what they do is call a model,
//! replay recorded calls and print. What they need is the store the sessions
//! wrote, read exactly as `mem` reads it — so they are given this store and
//! not a second implementation of it.
//!
//! Built only under the `python` feature, as `memory.so` on the tools' path.

use crate::{fm, index, text, vault};
use pyo3::exceptions::PyLookupError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::HashMap;
use std::path::PathBuf;

pyo3::create_exception!(memory, Ambiguous, PyLookupError);

fn ambiguous(e: vault::Ambiguous) -> PyErr {
    Ambiguous::new_err(e.to_string())
}

/// The frontmatter as a dict: every scalar field in the order the file holds
/// them, and `edges` when the node has any.
fn meta_dict<'py>(py: Python<'py>, meta: &fm::Meta) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    for (k, v) in &meta.fields {
        d.set_item(k, v)?;
    }
    if !meta.edges.is_empty() {
        let edges = PyList::empty(py);
        for e in &meta.edges {
            let one = PyDict::new(py);
            one.set_item("rel", &e.rel)?;
            one.set_item("to", &e.to)?;
            edges.append(one)?;
        }
        d.set_item("edges", edges)?;
    }
    Ok(d)
}

/// A dict back. Only the scalars are read: `edges` is a list, and nothing that
/// takes a meta dict here looks at it.
fn meta_from(d: &Bound<'_, PyDict>) -> fm::Meta {
    let mut m = fm::Meta::default();
    for (k, v) in d.iter() {
        if let (Ok(k), Ok(v)) = (k.extract::<String>(), v.extract::<String>()) {
            m.set(&k, v);
        }
    }
    m
}

#[pyclass(name = "Vault")]
pub struct PyVault {
    inner: vault::Vault,
}

#[pymethods]
impl PyVault {
    #[new]
    #[pyo3(signature = (root, oracle=None, oracle_order=None))]
    fn new(root: PathBuf, oracle: Option<PyRef<'_, PyVault>>,
           oracle_order: Option<HashMap<String, Vec<String>>>) -> Self {
        let mut v = vault::Vault::new(root);
        // an oracle is only ever read from, so it needs its root and nothing
        // else; an oracle's own oracle is never set
        v.oracle = oracle.map(|o| Box::new(vault::Vault::new(o.inner.root.clone())));
        v.oracle_order = oracle_order;
        PyVault { inner: v }
    }

    #[getter]
    fn root<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        py.import("pathlib")?.getattr("Path")?.call1((self.inner.root.clone(),))
    }

    /// (id, meta, body) for every node file. The id and the kind are the
    /// node's path, not anything the file holds, and are put in the meta dict
    /// so a caller reading nodes has them without splitting the id itself.
    fn nodes<'py>(&self, py: Python<'py>) -> PyResult<Vec<(String, Bound<'py, PyDict>, String)>> {
        self.inner.nodes().iter()
            .map(|n| {
                let d = meta_dict(py, &n.meta)?;
                d.set_item("id", &n.id)?;
                d.set_item("kind", n.kind())?;
                Ok((n.id.clone(), d, n.body.clone()))
            })
            .collect()
    }

    /// Where a node's file is. Asking creates the kind's directory, which is
    /// issue #14 and is reproduced rather than fixed.
    fn path_for<'py>(&self, py: Python<'py>, nid: &str) -> PyResult<Bound<'py, PyAny>> {
        py.import("pathlib")?.getattr("Path")?.call1((self.inner.path_for(nid),))
    }

    #[pyo3(signature = (r, kind=""))]
    fn resolve(&self, r: &str, kind: &str) -> PyResult<Option<String>> {
        self.inner.resolve(r, kind).map_err(ambiguous)
    }

    #[pyo3(signature = (name, kind=""))]
    fn by_name(&self, name: &str, kind: &str) -> PyResult<Option<String>> {
        self.inner.by_name(name, kind).map_err(ambiguous)
    }
}

#[pyfunction]
#[pyo3(signature = (text, root=None))]
fn fm_load<'py>(py: Python<'py>, text: &str, root: Option<PathBuf>)
    -> PyResult<(Bound<'py, PyDict>, String)>
{
    let (meta, body) = fm::load(text, root.as_deref());
    Ok((meta_dict(py, &meta)?, body))
}

#[pyfunction]
fn label(meta: &Bound<'_, PyDict>) -> String {
    fm::label(&meta_from(meta))
}

#[pyfunction]
fn live_body(body: &str) -> String {
    text::live_body(body)
}

#[pyfunction]
fn one_line(t: &str) -> String {
    text::one_line(t)
}

#[pyfunction]
#[pyo3(signature = (vault, date=""))]
fn seed(vault: PyRef<'_, PyVault>, date: &str) {
    index::seed(&vault.inner, date);
}

#[pyfunction]
fn regenerate_indexes(vault: PyRef<'_, PyVault>) -> PyResult<()> {
    index::regenerate_indexes(&vault.inner)
        .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
}

#[pyfunction]
fn write_graph_config(root: PathBuf) -> Vec<String> {
    index::write_graph_config(&root)
}

/// Where this module was loaded from. The importer sets `__file__` after the
/// module has run, so at this point the module object cannot say; the dynamic
/// linker can, given the address of anything inside it.
fn module_path() -> Option<PathBuf> {
    let mut info: libc::Dl_info = unsafe { std::mem::zeroed() };
    let here = module_path as *const libc::c_void;
    if unsafe { libc::dladdr(here, &mut info) } == 0 || info.dli_fname.is_null() {
        return None;
    }
    let name = unsafe { std::ffi::CStr::from_ptr(info.dli_fname) };
    Some(PathBuf::from(name.to_str().ok()?))
}

#[pymodule]
fn memory(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // the texts ship beside the module, as they ship beside the binary. Where
    // they do not — a checkout, where the module is in a build directory —
    // MEM_TEMPLATES names them, and a missing one is an error rather than an
    // empty instruction.
    if let Some(here) = module_path() {
        let beside = here.with_file_name("templates");
        if beside.is_dir() {
            index::set_templates(beside);
        }
    }
    m.add_class::<PyVault>()?;
    m.add("Ambiguous", m.py().get_type::<Ambiguous>())?;
    m.add("SUMMARY_MAX", crate::SUMMARY_MAX)?;
    m.add_function(wrap_pyfunction!(fm_load, m)?)?;
    m.add_function(wrap_pyfunction!(label, m)?)?;
    m.add_function(wrap_pyfunction!(live_body, m)?)?;
    m.add_function(wrap_pyfunction!(one_line, m)?)?;
    m.add_function(wrap_pyfunction!(seed, m)?)?;
    m.add_function(wrap_pyfunction!(regenerate_indexes, m)?)?;
    m.add_function(wrap_pyfunction!(write_graph_config, m)?)?;
    Ok(())
}
