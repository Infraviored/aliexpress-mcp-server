"""Bot check: the reason and the punish URL are kept; an image check points to captcha.py (Florian picks the tiles)."""
import json

import aliexpress_mcp_server as m
import captcha


def test_reason_kept_and_shown(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_BOT_FILE", tmp_path / "bot.json")
    monkeypatch.setattr(m, "_log_call", lambda kind: None)
    m.BOT_CHECK.clear()
    m._bot_note(True, "deny (no slider, only waiting helps)")
    assert "deny (no slider" in m.blocked_msg() and "import_profile" in m.blocked_msg()
    m._bot_note(False)
    assert m.blocked_msg() == ""


def test_image_check_points_to_captcha_py(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_BOT_FILE", tmp_path / "bot.json")
    monkeypatch.setattr(m, "_log_call", lambda kind: None)
    m.BOT_CHECK.clear()
    url = "https://x/_____tmd_____/punish?action=captcharecaptcha"
    m._bot_note(True, "image check", url=url)
    assert json.loads((tmp_path / "bot.json").read_text())["url"] == url
    msg = m.blocked_msg()
    assert "captcha.py start" in msg and "Do not pick the tiles yourself" in msg
    m._bot_note(False)


def test_answer_needs_a_waiting_check(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(captcha, "STATE", tmp_path / "state.json")
    assert captcha.answer(["2"]) == 1
    assert "no check is waiting" in capsys.readouterr().out


def test_numbers_drawn_on_tiles():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (300, 400), "white").save(buf, "PNG")
    tiles = [{"x": 10 + 100 * (i % 3), "y": 110 + 100 * (i // 3), "width": 95, "height": 95} for i in range(9)]
    out = Image.open(io.BytesIO(captcha._numbered(buf.getvalue(), tiles, {"x": 10, "y": 10})))
    assert out.getpixel((105, 105)) == (0, 0, 0)      # label box in tile 1's corner
