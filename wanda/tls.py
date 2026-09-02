from __future__ import annotations

import functools
import os
import ssl

import certifi


@functools.cache
def ssl_context() -> ssl.SSLContext:
    """A trust store that works regardless of how Python was installed.

    python.org's macOS framework build ships no CA bundle of its own — it
    points at etc/openssl/cert.pem inside the framework, which only exists
    after someone runs its "Install Certificates.command". Left alone, every
    TLS connection fails with CERTIFICATE_VERIFY_FAILED. Pinning certifi's
    bundle keeps the daemon working under launchd, where that machine-level
    setup is easy to miss and awkward to verify.

    SSL_CERT_FILE still wins, so a corporate root or a proxy CA can override.
    """
    cafile = os.environ.get("SSL_CERT_FILE") or certifi.where()
    return ssl.create_default_context(cafile=cafile)
