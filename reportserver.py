"""Serves the report pages (cart, plan, make_report) from a small local web server inside the MCP.

Nothing is published anywhere: the pages hold the user's cart and prices. The server listens on the
local network, every URL carries a long random token (stored mode 600), and only the report folder is
visible. Several MCP processes share one port: the first binds it, the others find it taken and reuse
the same address (same folder, same token).
"""
from __future__ import annotations

import http.server
import os
import secrets
import socket
import threading
from pathlib import Path

PORT = int(os.environ.get("ALIEXPRESS_REPORT_PORT", "8765"))
BIND = os.environ.get("ALIEXPRESS_REPORT_BIND", "0.0.0.0")      # 127.0.0.1 = this machine only
HOST = os.environ.get("ALIEXPRESS_REPORT_HOST", "")             # name or address the user's phone can reach
_started = False
_lock = threading.Lock()


def token(path: Path) -> str:
    """The URL token, created once (mode 600)."""
    try:
        t = path.read_text().strip()
        if len(t) >= 20:
            return t
    except OSError:
        pass
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    t = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t)
    return t


def _handler(directory: Path, tok: str):
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(directory), **k)

        def translate_path(self, path: str) -> str:
            parts = path.split("?", 1)[0].lstrip("/").split("/", 1)
            if parts[0] != tok or len(parts) < 2 or ".." in parts[1]:
                return str(directory / "__none__")          # not found, whatever was asked
            return super().translate_path("/" + parts[1])

        def list_directory(self, path):                     # no folder listings
            self.send_error(404)
            return None

        def log_message(self, *a):
            pass
    return H


def start(directory: Path, token_file: Path) -> bool:
    """Start the server thread once per process; False when the port is taken (another process serves it)."""
    global _started
    with _lock:
        if _started:
            return True
        directory.mkdir(parents=True, exist_ok=True)
        try:
            srv = http.server.ThreadingHTTPServer((BIND, PORT), _handler(directory, token(token_file)))
        except OSError:
            return False
        threading.Thread(target=srv.serve_forever, daemon=True, name="report-server").start()
        _started = True
        return True


def url(name: str, token_file: Path) -> str:
    host = HOST or socket.gethostname()
    return f"http://{host}:{PORT}/{token(token_file)}/{name}"
