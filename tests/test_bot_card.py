"""Bot check: the reason is logged and kept; a slider that cannot be passed sends Florian the card once."""
import aliexpress_mcp_server as m


def test_reason_kept_and_shown(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_BOT_FILE", tmp_path / "bot.json")
    monkeypatch.setattr(m, "_log_call", lambda kind: None)
    m.BOT_CHECK.clear()
    m._bot_note(True, "deny (no slider, only waiting helps)")
    assert "deny (no slider" in m.blocked_msg()
    m._bot_note(False)
    assert m.blocked_msg() == ""


def test_card_not_sent_twice(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_log_call", lambda kind: None)
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())  # one is open
    assert m._ask_user_card() is False and not calls
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1})())
    assert m._ask_user_card() is True and len(calls) == 1
