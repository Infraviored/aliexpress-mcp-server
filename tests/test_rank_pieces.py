"""search_products(pieces=N): hits ranked by net price per needed piece, not by single-piece price."""
import aliexpress_mcp_server as m


def _way(pieces, net, choice=True):
    return {"lines": [({"label": f"{pieces}PCS", "sku_id": "s1"}, 1)], "pieces": pieces, "goods": net - 3.57, "ship": 0.0,
            "duty": 3.57, "total": net, "net": net, "save": 0.0, "save_how": "", "days": 8,
            "offer": {"choice": choice}}


def _hits():
    return [{"item_id": "A", "title": "BH1750 GY-302 single", "price": 1.55, "original_price": None, "sold_count": "", "orders": 2942, "rating": 4.8},
            {"item_id": "B", "title": "BH1750 GY-302 pack", "price": 1.69, "original_price": None, "sold_count": "", "orders": 100, "rating": 4.7},
            {"item_id": "C", "title": "Light sensor BH1750 GY-302 bad", "price": 1.0, "original_price": None, "sold_count": "", "orders": 5, "rating": 4.0}]


def test_pack_ladder_beats_cheaper_single_piece(monkeypatch):
    ways = {"A": [_way(8, 15.06)], "B": [_way(10, 12.0)]}
    monkeypatch.setattr(m, "_ways_for", lambda i, spec, dc: (ways.get(i, []), [f"{i}: no data"]))
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    out = m._rank_for_pieces("BH1750", _hits(), 8, "GY-302", "bad", 12, False)
    assert out.index("item_id: B") < out.index("item_id: A")          # 1.20/pc beats 1.88/pc
    assert "item_id: C" not in out                                    # avoided by title
    assert "10 pcs" in out and "(1.20/pc)" in out


def test_nothing_priceable_says_so(monkeypatch):
    monkeypatch.setattr(m, "_ways_for", lambda i, spec, dc: ([], [f"{i}: no product data"]))
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    out = m._rank_for_pieces("x", _hits(), 3, "", "", 12, False)
    assert "could be priced" in out and "no product data" in out


def test_search_products_routes_to_ranking_only_for_several_pieces(monkeypatch):
    monkeypatch.setattr(m, "_search", lambda *a, **k: _hits())
    monkeypatch.setattr(m, "_rank_for_pieces", lambda *a: "RANKED")
    assert m.search_products("BH1750", pieces=5) == "RANKED"
    assert "RANKED" not in m.search_products("BH1750")


def test_bigger_pack_with_higher_total_does_not_win_on_unit_price(monkeypatch):
    ways = {"A": [_way(8, 12.0)], "B": [_way(20, 20.0)]}              # B is 1.00/pc but costs more
    monkeypatch.setattr(m, "_ways_for", lambda i, spec, dc: (ways.get(i, []), []))
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    out = m._rank_for_pieces("x", _hits(), 8, "GY-302", "bad", 12, False)
    assert out.index("item_id: A") < out.index("item_id: B")


def _line(**kw):
    return dict({"cart_id": "9", "item_id": "A", "sku_id": "s", "qty": 5, "variant": "1PCS", "title": "SHT41 sensor"}, **kw)


def test_saving_of_fifty_cents_is_reported_and_equal_is_not():
    row = (_way(5, 16.11), {"item_id": "B"})
    gain, best = m._saving([row], _way(5, 16.61))
    assert round(gain, 2) == 0.50 and best is row
    assert m._saving([row], _way(5, 16.11)) == (0.0, None)
    assert m._saving([], _way(5, 1.0)) == (0.0, None)


def test_search_with_cart_id_ends_with_verdict(monkeypatch):
    monkeypatch.setattr(m, "_search", lambda *a, **k: _hits())
    monkeypatch.setattr(m, "load_cookies", lambda: {"x": "y"})
    monkeypatch.setattr(m, "_read_cart", lambda c: {"lines": [_line()]})
    monkeypatch.setattr(m, "_line_store", lambda: {})
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    def ways(i, spec, dc):
        if spec.get("sku_id"):
            return [_way(5, 16.61)], []
        return {"A": [_way(5, 16.61)], "B": [_way(5, 16.11)]}.get(i, []), []
    monkeypatch.setattr(m, "_ways_for", ways)
    out = m.search_products("SHT41", cart_id="9")
    assert "CHEAPER THAN CART LINE 9 by 0.50" in out and "no trivial-saving threshold" in out
    assert "not in the cart" in m.search_products("SHT41", cart_id="404")


def test_recheck_cart_lists_swaps_and_cheapest(monkeypatch):
    lines = [_line(), _line(cart_id="10", item_id="X", title="Taster")]
    monkeypatch.setattr(m, "load_cookies", lambda: {"x": "y"})
    monkeypatch.setattr(m, "_read_cart", lambda c: {"lines": lines})
    monkeypatch.setattr(m, "_line_store", lambda: {"9": {"list": "L", "need": "SHT41"}, "10": {"list": "L", "need": "Taster"}})
    monkeypatch.setattr(m, "_search", lambda *a, **k: _hits())
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    def ways(i, spec, dc):
        if spec.get("sku_id"):
            return [_way(5, 10.0 if i == "X" else 16.61)], []
        return {"A": [_way(5, 16.61)], "B": [_way(5, 16.11)]}.get(i, []), []
    monkeypatch.setattr(m, "_ways_for", ways)
    out = m.recheck_cart(max_lines=5)
    assert "SAVES 0.50" in out and "remove_from_cart 9" in out
    assert "Taster (10): already cheapest" in out and "All listed lines are done." in out
    assert "skip=1" in m.recheck_cart(max_lines=1)
