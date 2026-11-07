"""The report server: pages only under the token, nothing else of the machine, and no publishing instruction."""
import urllib.error
import urllib.request

import aliexpress_mcp_server as m
import reportserver


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, ""


def test_server_serves_report_with_token_only(tmp_path, monkeypatch):
    monkeypatch.setattr(reportserver, "PORT", 0)
    monkeypatch.setattr(reportserver, "BIND", "127.0.0.1")
    monkeypatch.setattr(reportserver, "_started", False)
    (tmp_path / "cart.html").write_text("<p>cart</p>")
    (tmp_path.parent / "secret.txt").write_text("nope")
    tf = tmp_path / ".token"
    started = {}
    real = reportserver.http.server.ThreadingHTTPServer

    def spy(addr, handler):
        srv = real(addr, handler)
        started["port"] = srv.server_address[1]
        return srv
    monkeypatch.setattr(reportserver.http.server, "ThreadingHTTPServer", spy)
    assert reportserver.start(tmp_path, tf)
    tok = reportserver.token(tf)
    base = f"http://127.0.0.1:{started['port']}"
    assert _get(f"{base}/{tok}/cart.html") == (200, "<p>cart</p>")
    assert _get(f"{base}/cart.html")[0] == 404                      # no token
    assert _get(f"{base}/wrongtoken/cart.html")[0] == 404
    assert _get(f"{base}/{tok}/../secret.txt")[0] == 404           # no way out of the folder
    assert _get(f"{base}/{tok}/")[0] == 404                        # no folder listing
    assert oct(tf.stat().st_mode & 0o777) == "0o600"


def test_report_text_gives_an_address_and_never_says_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(m, "REPORT_TOKEN_FILE", tmp_path / ".token")
    monkeypatch.setattr(m.reportserver, "start", lambda *a: True)
    out = m._write_report("cart.html", "<p>x</p>")
    assert "http://" in out and "/cart.html" in out and "Artifact" not in out
    assert "Do not publish" in out


def test_call_log_has_a_line_per_call_and_the_bot_check(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "CALL_LOG", tmp_path / "calls.log")
    monkeypatch.setattr(m, "_mtop_pace_locked", lambda *a: None)
    monkeypatch.setattr(m, "_bot_note", m._bot_note)
    m._mtop_pace("search")
    m._log_call("BOT-CHECK")
    lines = (tmp_path / "calls.log").read_text().splitlines()
    assert lines[0].endswith("search") and lines[1].endswith("BOT-CHECK")
