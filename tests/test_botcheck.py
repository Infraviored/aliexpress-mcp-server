import json
import time

import aliexpress_mcp_server as m


def test_cooldown_blocks_after_bot_check_and_clears(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_BOT_FILE", tmp_path / "bot.json")
    m.BOT_CHECK.clear()
    assert m._bot_cooldown_left() == 0
    m._bot_note(True)
    assert 0 < m._bot_cooldown_left() <= m.BOT_COOLDOWN
    assert json.loads((tmp_path / "bot.json").read_text())["last"] <= time.time()
    m.BOT_CHECK.clear()  # a new process reads the file
    assert m._bot_cooldown_left() > 0
    m._bot_note(False)
    assert m._bot_cooldown_left() == 0 and not (tmp_path / "bot.json").exists()


def test_pdp_disk_cache_roundtrip_and_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_PDP_FILE", tmp_path / "pdp.json.gz")
    m._pdp_disk_store("1", {"ret": ["SUCCESS"], "data": {"a": 1}})
    assert m._pdp_disk_load()["1"]["resp"]["data"] == {"a": 1}
    monkeypatch.setattr(m, "PDP_DISK_TTL", -1)
    m._pdp_disk_store("2", {"x": 1})
    assert "1" not in m._pdp_disk_load()


def test_best_offer_packs_rule_and_warning(monkeypatch):
    offer = {"title": "Filament", "choice": True, "variants": [
        {"sku_id": "a", "label": "5 Black", "price": 50.0, "pack": 1, "unit": 50.0, "salable": True},
        {"sku_id": "b", "label": "1 Black", "price": 12.0, "pack": 1, "unit": 12.0, "salable": True}]}
    monkeypatch.setattr(m, "_pdp_offer", lambda _id: offer)
    monkeypatch.setattr(m, "_freight", lambda o: None)
    monkeypatch.setattr(m, "_discounts", lambda *a: [])
    f = getattr(m.best_offer, "fn", m.best_offer)
    assert "WARNING" in f("1", 5, "black")
    out = f("1", 5, "black", packs="5 black=5; 1 black=1")
    assert "WARNING" not in out and "5 pcs" in out and "50.00" in out
    assert "Bad packs rule" in f("1", 5, "black", packs="oops")
