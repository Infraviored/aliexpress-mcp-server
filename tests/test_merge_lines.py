import json

import aliexpress_mcp_server as m


def _msg(items):
    return "Auswahl X: ...\n```json\n" + json.dumps({"action": "select", "report": "x", "items": items}) + "\n```"


def _split_cart(monkeypatch, cands):
    l1 = {"cart_id": "11", "item_id": "100", "sku_id": "1", "title": "Radar HLK", "variant": "1 Stück", "price": 5.39, "qty": 4,
          "checked": True, "valid": True, "ship": 0, "group": "global", "choice": False, "image": "", "delivery": None}
    l2 = {"cart_id": "12", "item_id": "200", "sku_id": "2", "title": "Radar HLK 5er", "variant": "5PCS", "price": 30.19, "qty": 1,
          "checked": True, "valid": True, "ship": 0, "group": "global", "choice": False, "image": "", "delivery": None}
    tags = {"11": {"list": "Radar", "need": "Radar HLK-LD2450 (4 von 8)"}, "12": {"list": "Radar", "need": "Radar HLK-LD2450 (5 Stück)"}}
    monkeypatch.setattr(m, "load_cookies", lambda: {"a": "b"})
    monkeypatch.setattr(m, "_read_cart", lambda cookies, business="": {"lines": [l1, l2], "header": {}})
    monkeypatch.setattr(m, "_combo_lines", lambda cookies, cart: [])
    monkeypatch.setattr(m, "_line_store", lambda: tags)
    monkeypatch.setattr(m, "_alt_cache", lambda: {})
    monkeypatch.setattr(m, "_line_deal", lambda ln, tag, cache: None)
    monkeypatch.setattr(m, "_merge_candidate", lambda item_id, pieces, tag, cache: cands.get(item_id))
    monkeypatch.setattr(m, "_report_image", lambda card: "data:image/jpeg;base64,AAAA")
    written = {}
    monkeypatch.setattr(m, "_write_report", lambda name, page: (written.update(page=page), "ok")[1])
    return written


def _cand(item_id, sku, qty, cost):
    return {"item_id": item_id, "sku_id": sku, "qty": qty, "cost": cost, "buy": f"{qty}× x", "title": "T", "url": "https://x", "img": None}


def test_merges_split_lines_into_an_existing_line(monkeypatch):
    # 4 + 5 = 9 pieces: 9 singles of item 100 in one line (line 11, quantity change) beat the split (~52 + 2 duty)
    written = _split_cart(monkeypatch, {"100": _cand("100", "1", 9, 48.0), "200": _cand("200", "2", 2, 70.0)})
    out = m.decide_cart()
    p = written["page"]
    assert "MERGED LINES" in out and "saves" in out
    assert 'data-cart="11"' in p and 'data-qty0="4"' in p and 'value="9"' in p and "Zusammengelegt" in p
    assert 'data-cart="12"' in p and 'data-state0="yes"' in p
    assert p.count('value="delete" checked') == 1


def test_merge_into_a_new_offer_row_lists_all_old_ids(monkeypatch):
    written = _split_cart(monkeypatch, {"100": _cand("100", "9", 8, 40.0)})
    m.decide_cart()
    p = written["page"]
    assert 'data-replaces="11,12"' in p and 'data-sku="9"' in p


def test_split_lines_stay_when_the_split_is_cheapest(monkeypatch):
    written = _split_cart(monkeypatch, {"100": _cand("100", "1", 9, 60.0), "200": _cand("200", "2", 2, 70.0)})
    out = m.decide_cart()
    p = written["page"]
    assert "MERGED" not in out and "Zusammengelegt" not in p and 'value="delete" checked' not in p and 'data-qty0="4"' in p


def test_apply_selection_replaces_list_removes_all_after_add(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "add_to_cart", lambda *a, **k: "Added 8 x item")
    monkeypatch.setattr(m, "remove_from_cart", lambda ids: (calls.append(ids), "Removed")[1])
    m.apply_selection(_msg([{"item_id": "99", "sku_id": "77", "qty": 8, "state": "yes", "replaces": ["11", "12"]}]))
    assert calls == [["11", "12"]]
    calls.clear()
    m.apply_selection(_msg([{"item_id": "99", "sku_id": "77", "qty": 8, "state": "yes", "replaces": "11,12"}]))
    assert calls == [["11", "12"]]
    calls.clear()
    monkeypatch.setattr(m, "add_to_cart", lambda *a, **k: "Add to cart failed: boom")
    out = m.apply_selection(_msg([{"item_id": "99", "sku_id": "77", "qty": 8, "state": "yes", "replaces": ["11", "12"]}]))
    assert calls == [] and "NOT removed" in out
