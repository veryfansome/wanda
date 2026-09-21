fn main() {
    // The Python module finds its interpreter's symbols when it is loaded,
    // rather than linking a libpython at build time. macOS has to be told
    // that, and only for the cdylib — the rlib and `mem` link as usual.
    #[cfg(feature = "python")]
    pyo3_build_config::add_extension_module_link_args();
}
