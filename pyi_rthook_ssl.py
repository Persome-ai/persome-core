"""PyInstaller runtime hook: set SSL_CERT_FILE before anything imports ssl."""

import os
import sys

if not os.environ.get("SSL_CERT_FILE"):
    _meipass = getattr(sys, "_MEIPASS", None)
    if _meipass:
        _cacert = os.path.join(_meipass, "certifi", "cacert.pem")
        if os.path.isfile(_cacert):
            os.environ["SSL_CERT_FILE"] = _cacert
