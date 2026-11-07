"""search_products(pieces=N): hits ranked by net price per needed piece, not by single-piece price."""
import aliexpress_mcp_server as m


def _way(pieces, net, choice=True):
    return {"lines": [({"label": f"{pieces}PCS"}, 1)], "pieces": pieces, "goods": net - 3.57, "ship": 0.0,
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
