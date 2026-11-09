"""Inventory check before every add: the same item (any variant, either cart) or the same need is not added twice."""
import aliexpress_mcp_server as m


def _ln(cid, item, variant="EU Plug", qty=1, hs=""):
    return {"cart_id": cid, "item_id": item, "sku_id": "s" + cid, "qty": qty, "title": f"Charger {item}", "variant": variant,
            "valid": True, "checked": True, "hs": hs, "price": 5.0}


def _world(monkeypatch, main, combo, tags):
    monkeypatch.setattr(m, "load_cookies", lambda: {"x": "y"})
    monkeypatch.setattr(m, "_cart_inventory",
                        lambda ck: [dict(x, cart="main", tab="") for x in main] + [dict(x, cart="Kombi", tab="Tab A") for x in combo])
    monkeypatch.setattr(m, "_line_store", lambda: tags)
    added = []
    monkeypatch.setattr(m, "_cart_add", lambda ck, i, s, q, known=None, business="": (added.append((i, s, business)), "99")[1])
    monkeypatch.setattr(m, "_remember_lines", lambda *a, **k: None)
    monkeypatch.setattr(m, "_read_cart", lambda ck, business="": {"lines": [], "header": {}, "summary": {}})
    monkeypatch.setattr(m, "_cart_pages", lambda ck, business="": ({}, []))
    return added


def test_same_item_in_either_cart_other_sku_is_refused(monkeypatch):
    added = _world(monkeypatch, [_ln("1", "A", "US Plug")], [_ln("2", "A", "UK Plug", hs="x")], {})
    out = m.add_to_cart("A", "newsku", 1)
    assert out.startswith("ALREADY IN CART") and not added
    assert "main cart" in out and "Kombi-Knaller cart, tab Tab A" in out and "cart_id 1" in out and "cart_id 2" in out
    assert "US Plug" in out and "UK Plug" in out and "force=True" in out


def test_same_need_on_another_line_is_refused_and_force_adds(monkeypatch):
    added = _world(monkeypatch, [_ln("1", "A")], [], {"1": {"list": "L", "need": "Camera cover"}})
    out = m.add_to_cart("B", "s", 1, list_name="L", need="camera COVER")
    assert out.startswith("ALREADY IN CART") and "Camera cover" in out and not added
    m.add_to_cart("B", "s", 1, list_name="L", need="camera cover", force=True)
    assert added == [("B", "s", "")]


def test_a_new_item_and_need_is_added(monkeypatch):
    added = _world(monkeypatch, [_ln("1", "A")], [], {"1": {"list": "L", "need": "x"}})
    m.add_to_cart("B", "s", 1, need="y")
    assert added == [("B", "s", "")]


def test_unreadable_cart_adds_nothing(monkeypatch):
    added = _world(monkeypatch, [], [], {})
    monkeypatch.setattr(m, "_cart_inventory", lambda ck: (_ for _ in ()).throw(RuntimeError("boom")))
    out = m.add_to_cart("B", "s", 1)
    assert "Nothing added" in out and "force=True" in out and not added


def test_internal_replacing_callers_force_the_add(monkeypatch):
    import json
    seen = []
    monkeypatch.setattr(m, "add_to_cart", lambda i, s, q, list_name="", need="", combo=False, force=False: (seen.append((i, force)), "Added")[1])
    monkeypatch.setattr(m, "remove_from_cart", lambda ids: "Removed")
    sel = "```json\n" + json.dumps({"action": "select", "items": [
        {"item_id": "9", "sku_id": "8", "qty": 3, "state": "yes", "replaces": ["11"]},
        {"item_id": "7", "sku_id": "6", "qty": 1, "state": "yes"}]}) + "\n```"
    m.apply_selection(sel)
    assert seen == [("9", True), ("7", False)]


def test_feature_notes_flag_missing_match_and_avoided_words():
    lines = [{"cart_id": "1", "title": "65W GaN Charger", "variant": "US Plug"},
             {"cart_id": "2", "title": "65W GaN Charger", "variant": "EU Plug"},
             {"cart_id": "3", "title": "USB cable", "variant": "1m"}]
    tags = {"1": {"need": "Charger", "match": "EU", "avoid": "US, UK"}, "2": {"need": "Charger", "match": "eu", "avoid": "US, UK"},
            "3": {"need": "Cable", "avoid": "us"}}
    notes = m._feature_notes(lines, tags)
    assert any("cart_id 1" in n and "MISSING REQUIRED: eu" in n for n in notes)
    assert any("cart_id 1" in n and "HAS AVOIDED: us" in n for n in notes)
    assert not any("cart_id 2" in n or "cart_id 3" in n for n in notes)      # "us" is not in "USB"


def _offer(label, title="65W Charger"):
    return {"title": title, "variants": [{"sku_id": "s", "label": label}]}


def test_add_refuses_a_variant_without_match_word_or_with_avoided_word(monkeypatch):
    tags = {"1": {"list": "L", "need": "Charger", "match": "EU", "avoid": "US, UK"}}
    added = _world(monkeypatch, [], [], tags)
    monkeypatch.setattr(m, "_pdp_offer", lambda i: _offer("US Plug"))
    out = m.add_to_cart("B", "s", 1, list_name="L", need="Charger")
    assert out.startswith("WRONG VARIANT") and "MISSING REQUIRED: eu" in out and "HAS AVOIDED: us" in out and not added
    m.add_to_cart("B", "s", 1, list_name="L", need="Charger", force=True)
    assert added == [("B", "s", "")]
    added.clear()
    monkeypatch.setattr(m, "_pdp_offer", lambda i: _offer("EU Plug"))
    m.add_to_cart("B", "s", 1, list_name="L", need="Charger")
    assert added == [("B", "s", "")]


def test_duplicate_notes_name_items_in_several_lines_and_shared_needs():
    lines = [dict(_ln("1", "A"), cart="main", tab=""), dict(_ln("2", "A", "UK Plug"), cart="Kombi", tab="T"),
             dict(_ln("3", "B"), cart="main", tab=""), dict(_ln("4", "C"), cart="main", tab="")]
    tags = {"3": {"need": "Cover"}, "4": {"need": "cover"}}
    notes = m._duplicate_notes(lines, tags)
    assert any(n.startswith("Same item A in 2 lines") for n in notes)
    assert any("One need 'cover' on 2 lines" in n for n in notes)
    assert m._duplicate_notes(lines[2:3], tags) == []


def test_inventory_reads_kombi_cart_when_main_cart_is_empty(monkeypatch):
    """An empty main cart has no channel header; the Kombi cart must still be checked (live miss 2026-11-09)."""
    kombi = {"cart_id": "9", "item_id": "1005008868115239", "sku_id": "1", "qty": 4, "title": "Essager 30W",
             "variant": "White EU Plug", "hs": "8504409590"}
    monkeypatch.setattr(m, "_read_cart", lambda ck, b="": {"header": {}, "lines": [kombi] if b == m.COMBO else []})
    inv = m._cart_inventory({})
    assert [(x["cart"], x["cart_id"]) for x in inv] == [("Kombi", "9")]
    assert m._combo_lines({}, {"header": {}, "lines": []}) == [kombi]
