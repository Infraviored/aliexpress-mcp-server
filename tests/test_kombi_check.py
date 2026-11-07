"""kombi_check (which lines are cheaper as Kombi-Knaller, split into orders) and the paced, cached search."""
import aliexpress_mcp_server as m


def _way(pieces, net, combo=True, free=False, duty_back=3.57):
    return {"lines": [({"label": "x", "sku_id": "s"}, 1)], "pieces": pieces, "net": net, "goods": net, "save": 0.0, "save_how": "",
            "days": 9, "duty": 0.0, "combo": combo, "combo_free": free, "duty_back": duty_back, "offer": {"choice": False}}


def _line(cid, item, qty=3):
    return {"cart_id": cid, "item_id": item, "sku_id": "s", "qty": qty, "variant": "1PCS", "title": f"title {item}"}


def _world(monkeypatch, tags):
    lines = [_line("1", "A"), _line("2", "B"), _line("3", "C")]
    monkeypatch.setattr(m, "load_cookies", lambda: {"x": "y"})
    monkeypatch.setattr(m, "_read_cart", lambda c, business="": {"lines": lines})
    monkeypatch.setattr(m, "_line_store", lambda: tags)
    monkeypatch.setattr(m, "_search", lambda q, *a, **k: [{"item_id": "K" + q, "title": f"kombi {q}", "combo": q != "none"}])
    monkeypatch.setattr(m, "_price_hits", lambda hits, p, ma, av, rt, co: ([(_way(p, 10.0), hits[0])], 1, []))
    monkeypatch.setattr(m, "_current_way", lambda ln, p: {"net": {"A": 20.0, "B": 10.5, "C": 5.0}[ln["item_id"]], "pieces": p})


def test_kombi_check_lists_wins_without_import_charge_and_orders(monkeypatch):
    tags = {"1": {"list": "L", "need": "a", "match": "w"}, "2": {"list": "L", "need": "b", "match": "w"},
            "3": {"list": "L", "need": "none", "match": "w"}}
    _world(monkeypatch, tags)
    out = m._kombi_run("L", 6, 12, 0, lambda *a: None)
    assert "KOMBI IS CHEAPER for 2 line(s)" in out            # A: 20.00 -> 10.00 - 3.57 = 6.43 (not free yet: duty taken off)
    assert "a x3 (cart_id 1)" in out and "combo=True" in out and "remove_from_cart 1" in out
    assert "Order 1:" in out and "none (3)" in out             # C has no Kombi hit


def test_kombi_check_without_match_words_is_unsure(monkeypatch):
    _world(monkeypatch, {"1": {"list": "L", "need": "a"}})
    out = m._kombi_run("L", 6, 12, 0, lambda *a: None)
    assert "UNSURE" in out and "No line is cheaper as Kombi" in out and "combo=True" not in out


def test_kombi_orders_split_by_limit_and_keep_three_pieces_each():
    ps = [{"pieces": p} for p in (5, 4, 2, 1)]
    orders = m._kombi_orders(ps, 2)
    assert len(orders) == 2 and all(sum(x["pieces"] for x in o) >= 3 for o in orders)
    assert len(m._kombi_orders([{"pieces": 2}, {"pieces": 1}], 1)) == 1      # a short order joins the one before


def test_search_is_paced_cached_and_stops_in_a_bot_check(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_mtop_pace", lambda *a: calls.append("pace"))
    monkeypatch.setattr(m, "_bot_cooldown_left", lambda: 0.0)
    monkeypatch.setattr(m, "parse_search_results", lambda html: [{"item_id": "1", "title": "t"}])
    m._SEARCH_CACHE.clear()

    class Resp:
        url = "https://www.aliexpress.com/w/x.html"
        text = "<html>"
        def raise_for_status(self): pass

    class Client:
        def get(self, *a, **k): calls.append("get"); return Resp()
        def close(self): pass
    monkeypatch.setattr(m, "get_client", lambda *a, **k: Client())
    assert m._search("BH1750")[0]["item_id"] == "1" and m._search("bh1750 ")[0]["item_id"] == "1"
    assert calls == ["pace", "get"]                              # the second call came from the cache
    Resp.url = "https://www.aliexpress.com/_____tmd_____/punish?x"
    noted = []
    monkeypatch.setattr(m, "_bot_note", lambda b: noted.append(b))
    try:
        m._search("other")
        raise AssertionError("no bot check raised")
    except RuntimeError as e:
        assert "bot check" in str(e) and noted == [True]
    monkeypatch.setattr(m, "_bot_cooldown_left", lambda: 300.0)
    try:
        m._search("third")
        raise AssertionError("cooldown ignored")
    except RuntimeError as e:
        assert "paused" in str(e)


def test_orders_below_three_pieces_are_merged_until_every_order_has_three():
    orders = m._kombi_orders([{"pieces": 1}, {"pieces": 1}, {"pieces": 1}], 1)
    assert len(orders) == 1 and sum(x["pieces"] for x in orders[0]) == 3
    orders = m._kombi_orders([{"pieces": 5}, {"pieces": 1}, {"pieces": 1}, {"pieces": 1}], 1)
    assert all(sum(x["pieces"] for x in o) >= 3 for o in orders)


def test_x5secdata_on_a_normal_page_is_no_bot_check(monkeypatch):
    monkeypatch.setattr(m, "_mtop_pace", lambda *a: None)
    monkeypatch.setattr(m, "_bot_cooldown_left", lambda: 0.0)
    monkeypatch.setattr(m, "parse_search_results", lambda html: [{"item_id": "1", "title": "t"}])
    m._SEARCH_CACHE.clear()

    class Resp:
        url = "https://www.aliexpress.com/w/x.html"
        text = "<script>var x5secdata='abc'</script>"
        def raise_for_status(self): pass

    class Client:
        def get(self, *a, **k): return Resp()
        def close(self): pass
    monkeypatch.setattr(m, "get_client", lambda *a, **k: Client())
    assert m._search("normal")[0]["item_id"] == "1"
