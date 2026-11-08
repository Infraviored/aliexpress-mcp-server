"""Serves the report pages (cart, plan, make_report) from a small local web server inside the MCP.

Nothing is published anywhere: the pages hold the user's cart and prices. The server listens on the
local network, every URL carries a long random token (stored mode 600), and only the report folder is
visible. Several MCP processes share one port: the first binds it, the others find it taken and reuse
the same address (same folder, same token); `alive()` checks that the one answering is ours.

Run it as its own service (`python reportserver.py`, unit file in deploy/) so the address keeps working
when the MCP process restarts: a thread inside the MCP dies with it, and every reconnect killed the links.
"""
from __future__ import annotations

import http.server
import os
import secrets
import socket
import threading
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlsplit

PORT = int(os.environ.get("ALIEXPRESS_REPORT_PORT", "8765"))
BIND = os.environ.get("ALIEXPRESS_REPORT_BIND", "0.0.0.0")      # 127.0.0.1 = this machine only
HOST = os.environ.get("ALIEXPRESS_REPORT_HOST", "")             # name or address the user's phone can reach
# Same defaults as the MCP: the pages it writes and the token it hands out.
REPORT_DIR = Path(os.environ.get("ALIEXPRESS_REPORT_DIR", "~/aliexpress-reports")).expanduser()
TOKEN_FILE = Path(os.environ.get("ALIEXPRESS_CREDENTIALS", "~/.mcp-credentials/aliexpress.json")).expanduser().parent \
    / "aliexpress-report-token"
_started = False
_lock = threading.Lock()


def token(path: Path) -> str:
    """The URL token, created once (mode 600). Exclusive creation: two processes never end up with two tokens."""
    for _ in range(3):
        try:
            t = path.read_text().strip()
            if len(t) >= 20:
                return t
        except OSError:
            pass
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:                 # the other process is writing it: read it next round
            threading.Event().wait(0.05)
            continue
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(24))
    return path.read_text().strip()


def safe_file(directory: Path, rel: str) -> Path | None:
    """The file `rel` names inside `directory` (after URL decoding), or None: no ../, no sub folders' escape, no symlink out."""
    rel = unquote(rel)
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        return None
    root = directory.resolve()
    target = (root / rel).resolve()
    if root not in target.parents or not target.is_file():
        return None
    return target


def _handler(directory: Path, tok: str):
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(directory), **k)

        def _target(self) -> Path | None:
            path = urlsplit(self.path).path             # also the absolute form "http://host/..."
            first, _, rest = path.lstrip("/").partition("/")
            if first != tok:
                return None
            if rest == ".ping":
                return Path("/.ping")
            return safe_file(directory, rest)

        def _serve(self, head: bool) -> None:
            t = self._target()
            if t is None:
                self.send_error(404)
                return
            if t == Path("/.ping"):
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not head:
                    self.wfile.write(body)
                return
            self.path = "/" + t.relative_to(directory.resolve()).as_posix()
            (super().do_HEAD if head else super().do_GET)()

        def do_GET(self):
            self._serve(False)

        def do_HEAD(self):
            self._serve(True)

        def log_message(self, *a):
            pass
    return H


def server(directory: Path, token_file: Path, port: int | None = None) -> http.server.ThreadingHTTPServer:
    directory.mkdir(parents=True, exist_ok=True)
    return http.server.ThreadingHTTPServer((BIND, PORT if port is None else port), _handler(directory, token(token_file)))


def start(directory: Path, token_file: Path) -> bool:
    """Start the server thread once per process; False when the port is taken (another process may serve it)."""
    global _started
    with _lock:
        if _started:
            return True
        try:
            srv = server(directory, token_file)
        except OSError:
            return False
        threading.Thread(target=srv.serve_forever, daemon=True, name="report-server").start()
        _started = True
        return True


def alive(token_file: Path) -> bool:
    """Is it our report server that answers on the port (a foreign service does not know the token)?"""
    try:
        url = f"http://127.0.0.1:{PORT}/{token(token_file)}/.ping"
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.read() == b"ok"
    except Exception:
        return False


def url(name: str, token_file: Path) -> str:
    host = HOST or ("127.0.0.1" if BIND in ("127.0.0.1", "localhost") else socket.gethostname())
    return f"http://{host}:{PORT}/{token(token_file)}/{name}"


if __name__ == "__main__":
    # The service: serve the report folder in the foreground. A port still held by an MCP thread makes
    # this exit; systemd starts it again a few seconds later and it takes over once that process is gone.
    s = server(REPORT_DIR, TOKEN_FILE)
    print(f"report server on {BIND}:{PORT}, folder {REPORT_DIR}", flush=True)
    s.serve_forever()
