"""The MCP must never work as a guest: every cart tool checks the login first and logs in by itself."""
import subprocess

import aliexpress_mcp_server as m


def _expired(*a, **k):
    raise PermissionError("auth expired")


def test_session_alive_false_when_aliexpress_says_expired(monkeypatch):
    monkeypatch.setattr(m, "load_cookies", lambda: {"x": "1"})
    monkeypatch.setattr(m, "_orders_call", _expired)
    monkeypatch.setattr(m, "_SESSION_OK_UNTIL", 0.0)
    assert m.session_alive() is False


def test_session_alive_trusts_a_network_error(monkeypatch):
    monkeypatch.setattr(m, "load_cookies", lambda: {"x": "1"})
    monkeypatch.setattr(m, "_orders_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(m, "_SESSION_OK_UNTIL", 0.0)
    assert m.session_alive() is True            # not a login problem: do not start a browser for it


def test_session_alive_caches_a_proven_login(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "load_cookies", lambda: {"x": "1"})
    monkeypatch.setattr(m, "_orders_call", lambda *a, **k: calls.append(1) or {})
    monkeypatch.setattr(m, "_SESSION_OK_UNTIL", 0.0)
    assert m.session_alive() and m.session_alive()
    assert len(calls) == 1


def test_guard_logs_in_by_itself_and_then_passes(monkeypatch):
    state = {"alive": False, "ran": 0}

    def fake_run(cmd, **k):
        state["ran"] += 1
        state["alive"] = True
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(m, "session_alive", lambda force=False: state["alive"])
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    assert m._login_guard() is None
    assert state["ran"] == 1


def test_guard_reports_when_the_auto_login_fails(monkeypatch):
    monkeypatch.setattr(m, "session_alive", lambda force=False: False)
    monkeypatch.setattr(m.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "no saved password"))
    msg = m._login_guard()
    assert msg and "Auto-login failed: no saved password" in msg and "import_profile.py --ask" in msg


def test_cart_tools_check_the_login_first(monkeypatch):
    monkeypatch.setattr(m, "_login_guard", lambda: "LOGIN NEEDED")
    assert m.view_cart() == "LOGIN NEEDED"
    assert m.select_cart_items(["1"]) == "LOGIN NEEDED"
    assert m.add_to_cart("1", "2") == "LOGIN NEEDED"
    assert m.apply_selection("x") == "LOGIN NEEDED"
