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


def test_duplicate_notes_name_items_in_several_lines_and_shared_needs():
    lines = [dict(_ln("1", "A"), cart="main", tab=""), dict(_ln("2", "A", "UK Plug"), cart="Kombi", tab="T"),
             dict(_ln("3", "B"), cart="main", tab=""), dict(_ln("4", "C"), cart="main", tab="")]
    tags = {"3": {"need": "Cover"}, "4": {"need": "cover"}}
    notes = m._duplicate_notes(lines, tags)
    assert any(n.startswith("Same item A in 2 lines") for n in notes)
    assert any("One need 'cover' on 2 lines" in n for n in notes)
    assert m._duplicate_notes(lines[2:3], tags) == []
