"""PyInstaller entry point for the Typer CLI.

``SSL_CERT_FILE`` must be set before imports so HTTPS clients can locate the
bundled CA certificates in a frozen application.
"""

import os
import sys

if not os.environ.get("SSL_CERT_FILE") and getattr(sys, "frozen", False):
    _cacert = os.path.join(getattr(sys, "_MEIPASS", ""), "certifi", "cacert.pem")
    if os.path.isfile(_cacert):
        os.environ["SSL_CERT_FILE"] = _cacert

from persome.cli import app

if __name__ == "__main__":
    app()
