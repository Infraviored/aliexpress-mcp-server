import json

import aliexpress_mcp_server as m

LINE = {"cart_id": "11", "item_id": "1", "sku_id": "2", "title": "Sensor", "variant": "Purple", "price": 14.0, "qty": 3,
        "checked": True, "valid": True, "ship": 0, "group": "group_choice", "choice": True, "image": "", "delivery": None}


def _cand(cost, combo=False, pieces=3, choice=False):
    return {"item_id": "99", "title": "Sensor other", "url": "https://x/99", "img": None, "buy": "3× Purple", "cost": cost,
            "days": 5, "sku_id": "77", "qty": 3, "ambiguous": False, "combo": combo, "pieces": pieces, "choice": choice,
            "lines": [{"sku_id": "77", "qty": 3}]}


def _cart(monkeypatch, deal, combo_lines=(), match="Sensor"):
    monkeypatch.setattr(m, "load_cookies", lambda: {"a": "b"})
    monkeypatch.setattr(m, "_read_cart", lambda cookies, business="": {"lines": [dict(LINE)], "header": {}})
    monkeypatch.setattr(m, "_combo_lines", lambda cookies, cart: list(combo_lines))
    monkeypatch.setattr(m, "_line_store", lambda: {"11": {"list": "L", "need": "Sensor", "match": match}})
    monkeypatch.setattr(m, "_alt_cache", lambda: {})
    monkeypatch.setattr(m, "_line_deal", lambda ln, tag, cache: deal)
    monkeypatch.setattr(m, "_report_image", lambda card: "data:image/jpeg;base64,AAAA")
    monkeypatch.setattr(m, "_compare_image", lambda *a, **k: "")
    written = {}
    monkeypatch.setattr(m, "_write_report", lambda name, page: (written.update(page=page), "ok")[1])
    return written


def test_cheaper_other_seller_for_a_choice_line_becomes_a_pre_selected_card(monkeypatch):
    written = _cart(monkeypatch, {"now": 50.0, "best": _cand(40.0, choice=True), "kombi": None})
    out = m.decide_cart(approved_alternatives=["11"])
    p = written["page"]
    assert 'class="pill save">spart' in p and 'data-replaces="11"' in p and "data-combo" not in p
    assert p.count('value="yes" checked') == 1 and p.count('value="no" checked') == 1   # new row yes, the old line no
    assert "BETTER DEALS FOUND" in out and "on the page" in out


def test_a_more_expensive_offer_is_not_shown_or_listed(monkeypatch):
    written = _cart(monkeypatch, {"now": 50.0, "best": _cand(55.0), "kombi": None})
    out = m.decide_cart(approved_alternatives=["11"])
    assert "data-replaces" not in written["page"] and "BETTER DEALS" not in out and "NOT ON THE PAGE" not in out


def test_unapproved_is_pending_and_says_when_match_words_are_missing(monkeypatch):
    written = _cart(monkeypatch, {"now": 50.0, "best": _cand(40.0), "kombi": None}, match="")
    out = m.decide_cart()
    assert "data-replaces" not in written["page"] and "NOT ON THE PAGE YET" in out and "no match words" in out.lower()


def test_kombi_wins_only_with_three_combo_pieces(monkeypatch):
    written = _cart(monkeypatch, {"now": 50.0, "best": _cand(52.0), "kombi": _cand(45.0, combo=True, pieces=3)})
    m.decide_cart(approved_alternatives=["11"])
    p = written["page"]
    assert 'data-combo="1"' in p and 'class="pill info">Kombi-Knaller' in p and "ohne Abgabe" in p
    written = _cart(monkeypatch, {"now": 50.0, "best": _cand(52.0), "kombi": _cand(45.0, combo=True, pieces=2)})
    m.decide_cart(approved_alternatives=["11"])
    assert "data-replaces" not in written["page"]                  # 2 pieces: its normal net (52) is not cheaper
    written = _cart(monkeypatch, {"now": 50.0, "best": _cand(48.0), "kombi": _cand(45.0, combo=True, pieces=2)})
    m.decide_cart(approved_alternatives=["11"])
    assert 'data-replaces="11"' in written["page"] and "data-combo" not in written["page"]   # falls back to best


def test_combo_cart_pieces_count_towards_the_three(monkeypatch):
    other = dict(LINE, cart_id="12", item_id="5", sku_id="6", qty=2, variant="")
    written = _cart(monkeypatch, {"now": 50.0, "best": _cand(52.0), "kombi": _cand(45.0, combo=True, pieces=1)}, combo_lines=[other])
    m.decide_cart(approved_alternatives=["11"])
    assert 'data-combo="1"' in written["page"]


def test_apply_selection_passes_combo_to_add_to_cart(monkeypatch):
    seen = []
    monkeypatch.setattr(m, "add_to_cart", lambda i, s, q, list_name="", need="", combo=False: (seen.append((i, combo)), "Added")[1])
    monkeypatch.setattr(m, "remove_from_cart", lambda ids: "Removed")
    sel = "```json\n" + json.dumps({"action": "select", "items": [
        {"item_id": "9", "sku_id": "8", "qty": 3, "state": "yes", "combo": True, "replaces": ["11"]},
        {"item_id": "7", "sku_id": "6", "qty": 1, "state": "yes"}]}) + "\n```"
    m.apply_selection(sel)
    assert seen == [("9", True), ("7", False)]


def _way(item, sku, net, pieces=3, combo_free=False, duty_back=0.0, choice=False):
    return {"offer": {"item_id": item, "title": "T", "url": "u", "image": None, "choice": choice}, "net": net, "pieces": pieces,
            "lines": [({"sku_id": sku, "label": "Purple", "image": None}, pieces)], "days": 5, "duty_back": duty_back,
            "combo_free": combo_free}


def test_line_deal_prices_best_and_kombi_and_skips_the_line_itself(monkeypatch):
    rows = [(_way("1", "2", 30.0), {"combo": False}),                       # the line itself
            (_way("5", "6", 44.0, choice=True), {"combo": False}),
            (_way("7", "8", 47.0, duty_back=3.0), {"combo": True}),
            (_way("9", "10", 20.0, pieces=2), {"combo": False})]            # too few pieces
    monkeypatch.setattr(m, "_current_way", lambda ln, pieces: {"net": 50.0, "pieces": pieces})
    monkeypatch.setattr(m, "_search", lambda q: [])
    monkeypatch.setattr(m, "_price_hits", lambda *a, **k: (rows, 4, []))
    cache = {}
    d = m._line_deal(dict(LINE), {"match": "x"}, cache)
    assert d["now"] == 50.0 and d["best"]["item_id"] == "5" and d["best"]["choice"] and d["best"]["cost"] == 44.0
    assert d["kombi"]["item_id"] == "7" and d["kombi"]["cost"] == 44.0 and d["kombi"]["combo"]
    assert len(cache) == 1 and next(iter(cache)).startswith("deal:1:2:3:")
    monkeypatch.setattr(m, "_current_way", lambda *a: (_ for _ in ()).throw(AssertionError("cached")))
    assert m._line_deal(dict(LINE), {"match": "x"}, cache) == d
