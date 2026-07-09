"""PyInstaller 入口。`persome.cli:app` 是一个 typer App，包装一下即可。

SSL_CERT_FILE 必须在所有 import 之前设置，否则 PyInstaller 打包环境中
HTTPS 客户端（anthropic SDK / httpx）会因找不到系统 CA 证书而 SSL 握手失败。
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
