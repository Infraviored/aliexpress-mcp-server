"""Planning with Kombi-Knaller: no import charge from 3 combo pieces, charge back when fewer, own order in the plan."""
import pytest

import aliexpress_mcp_server as m


def _offer(item_id, price, choice=True):
    return {"item_id": item_id, "title": f"Part {item_id}", "choice": choice, "store": "S" + item_id,
            "shipping": {"days": (5, 8)}, "url": "https://x/" + item_id,
            "variants": [{"label": "1PCS", "sku_id": "s" + item_id, "price": price, "salable": True,
                          "pack": 1, "ambiguous": False}]}


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    offers = {"A": _offer("A", 2.0), "B": _offer("B", 2.0), "N": _offer("N", 2.5)}
    monkeypatch.setattr(m, "_pdp_offer", lambda i: offers.get(i))
    monkeypatch.setattr(m, "_freight", lambda offer, sku=None: None)
    monkeypatch.setattr(m, "_discounts", lambda i, s: None)
    monkeypatch.setattr(m, "DUTY_PER_LINE", 3.57)
    m.COMBO_ITEMS.clear()
    yield
    m.COMBO_ITEMS.clear()


def test_three_pieces_of_one_combo_item_carry_no_duty():
    m.COMBO_ITEMS.add("A")
    ways, _ = m._ways_for("A", {"pieces": 3}, 0.0)
    w = ways[0]
    assert w["combo"] and w["combo_free"] and w["duty"] == 0.0
    assert round(w["net"], 2) == 6.0                      # goods only


def test_two_pieces_alone_keep_the_duty_but_stay_a_combo_candidate():
    m.COMBO_ITEMS.add("A")
    w = m._ways_for("A", {"pieces": 2}, 0.0)[0][0]
    assert w["combo"] and not w.get("combo_free")
    assert round(w["net"], 2) == 4.0 + 3.57
    m._combo_free(w, True)
    assert round(w["net"], 2) == 4.0
    m._combo_free(w, False)
    assert round(w["net"], 2) == 4.0 + 3.57


def test_plan_joins_two_small_combo_items_into_one_combo_order(monkeypatch):
    m.COMBO_ITEMS.update({"A", "B"})
    specs = [{"need": "a", "item_ids": ["A"], "pieces": 2}, {"need": "b", "item_ids": ["B"], "pieces": 1}]
    plan = m._plan(specs, 0.0)
    assert plan["combo_pieces"] == 3 and plan["combo_lines"] == 2
    assert plan["duty"] == 0 and round(plan["total"], 2) == 6.0
    text = m._fmt_plan(plan)
    assert "KOMBI-KNALLER" in text and "Kombi-Knaller order" in text and "no import charge" in text


def test_too_few_combo_pieces_get_the_duty_back():
    m.COMBO_ITEMS.add("A")
    plan = m._plan([{"need": "a", "item_ids": ["A"], "pieces": 2}], 0.0)
    assert plan["combo_pieces"] == 0
    assert not plan["picks"][0]["way"].get("combo")
    assert round(plan["total"], 2) == 4.0 + 3.57


def test_non_combo_item_is_unchanged():
    plan = m._plan([{"need": "n", "item_ids": ["N"], "pieces": 4}], 0.0)
    assert plan["combo_pieces"] == 0 and round(plan["total"], 2) == 10.0 + 3.57


def test_dearer_combo_does_not_win_when_the_charge_is_not_saved():
    # B (combo, 2.0) and N (not combo, 2.5) compete for 2 pieces: one combo item alone gets no benefit
    m.COMBO_ITEMS.add("B")
    plan = m._plan([{"need": "x", "item_ids": ["B", "N"], "pieces": 2}], 0.0)
    assert plan["picks"][0]["way"]["offer"]["item_id"] == "B"        # still cheaper per goods
    assert plan["combo_pieces"] == 0 and plan["duty"] > 0


def test_combo_lines_split_into_rounds_when_a_limit_is_set(monkeypatch):
    m.COMBO_ITEMS.update({"A", "B"})
    monkeypatch.setattr(m, "COMBO_MAX_LINES", 1)
    plan = m._plan([{"need": "a", "item_ids": ["A"], "pieces": 3}, {"need": "b", "item_ids": ["B"], "pieces": 3}], 0.0)
    assert "2 rounds" in m._combo_order_text(plan)


def test_three_pack_stays_free_when_a_single_piece_variant_comes_first(monkeypatch):
    offer = _offer("P", 2.0)
    offer["variants"].append({"label": "3PCS", "sku_id": "sP3", "price": 5.0, "salable": True, "pack": 3, "ambiguous": False})
    monkeypatch.setattr(m, "_pdp_offer", lambda i: offer)
    m.COMBO_ITEMS.add("P")
    plan = m._plan([{"need": "p", "item_ids": ["P"], "pieces": 3}], 0.0)
    w = plan["picks"][0]["way"]
    assert w["pieces"] >= 3 and w.get("combo_free") and plan["duty"] == 0


def test_apply_with_only_combo_lines_leaves_the_main_cart_selection_alone(monkeypatch):
    m.COMBO_ITEMS.update({"A", "B"})
    plan = m._plan([{"need": "a", "item_ids": ["A"], "pieces": 2}, {"need": "b", "item_ids": ["B"], "pieces": 1}], 0.0)
    plan["list"] = "L"
    calls = []
    monkeypatch.setattr(m, "_read_cart", lambda c, business="": {"lines": [], "header": {}})
    monkeypatch.setattr(m, "_line_store", lambda: {})
    monkeypatch.setattr(m, "_remember_lines", lambda *a, **k: None)
    monkeypatch.setattr(m, "_cart_add", lambda *a, **k: "99")
    monkeypatch.setattr(m, "_select", lambda *a, **k: calls.append(a) or ("", {"lines": []}))
    out = m._apply_plan({"x": "y"}, plan)
    assert not calls and "selection is unchanged" in out and "Kombi: added" in out
