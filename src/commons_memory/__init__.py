"""Commons: unified cross-AI memory hub (MCP gateway + SQLite store)."""
__version__ = "0.22.1"


def _ca_bundle() -> None:
    """python.org macOS builds ship without a CA bundle until 'Install Certificates' is run, so every HTTPS call
    (providers, research, webhooks, importers) failed with CERTIFICATE_VERIFY_FAILED. Use certifi only when the
    interpreter has no CA file or dir at all; a working system or corporate CA setup is left alone."""
    import os
    import ssl
    p = ssl.get_default_verify_paths()
    if os.environ.get("SSL_CERT_FILE") or any(x and os.path.exists(x) for x in (p.cafile, p.capath, p.openssl_cafile,
                                                                                 p.openssl_capath)):
        return
    try:
        import certifi
    except ImportError:
        return
    os.environ["SSL_CERT_FILE"] = certifi.where()


_ca_bundle()
