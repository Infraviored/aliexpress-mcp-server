"""Kombi lines are spread over rounds under a plan id; combo_round puts the next round in and checks only it."""
import aliexpress_mcp_server as m
from tests.test_combo_plan import _offer


def _picks(monkeypatch, n=3, limit=2):
    offers = {c: _offer(c, 2.0) for c in "ABC"}
    monkeypatch.setattr(m, "_pdp_offer", lambda i: offers.get(i))
    monkeypatch.setattr(m, "_freight", lambda offer, sku=None: None)
    monkeypatch.setattr(m, "_discounts", lambda i, s: None)
    monkeypatch.setattr(m, "COMBO_MAX_LINES", limit)
    m.COMBO_ITEMS.update("ABC")
    return m._plan([{"need": c.lower(), "item_ids": [c], "pieces": 3} for c in "ABC"][:n], 0.0)


def test_picks_are_spread_over_rounds_and_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "PLANS_FILE", tmp_path / "plans.json")
    plan = _picks(monkeypatch)
    plan["list"] = "L"
    m._attach_rounds(plan)
    assert len(plan["rounds"]) == 2 and plan["id"].startswith("plan-")
    assert all(sum(p["way"]["pieces"] for p in r) >= 3 for r in plan["rounds"])
    stored = m._plan_store()[plan["id"]]
    assert stored["list"] == "L" and len(stored["rounds"]) == 2 and not stored["rounds"][0]["added"]
    assert "Round 1:" in m._combo_order_text(plan) and "combo_round" in m._combo_order_text(plan)


def test_apply_puts_only_round_one_into_the_combo_cart(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "PLANS_FILE", tmp_path / "plans.json")
    plan = _picks(monkeypatch)
    plan["list"] = "L"
    m._attach_rounds(plan)
    added = []
    monkeypatch.setattr(m, "_read_cart", lambda c, business="": {"lines": [], "header": {}})
    monkeypatch.setattr(m, "_line_store", lambda: {})
    monkeypatch.setattr(m, "_remember_lines", lambda *a, **k: None)
    monkeypatch.setattr(m, "_cart_add", lambda ck, iid, sku, qty, **k: added.append(iid) or "9")
    monkeypatch.setattr(m, "_select", lambda *a, **k: ("", {"lines": []}))
    m._apply_plan({"x": "y"}, plan)
    assert sorted(added) == sorted(p["way"]["offer"]["item_id"] for p in plan["rounds"][0])


def test_combo_round_adds_the_next_round_and_checks_only_it(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "PLANS_FILE", tmp_path / "plans.json")
    plan = _picks(monkeypatch)
    plan["list"] = "L"
    m._attach_rounds(plan)
    cart = {}
    calls = []
    monkeypatch.setattr(m, "load_cookies", lambda: {"x": "y"})

    def read(ck, business=""):
        return {"lines": [{"cart_id": f"c{k[0]}", "item_id": k[0], "sku_id": k[1], "valid": True, "checked": True,
                           "qty": q, "price": 2.0} for k, q in cart.items()], "header": {}, "summary": {}}
    monkeypatch.setattr(m, "_read_cart", read)
    monkeypatch.setattr(m, "_line_store", lambda: {})
    monkeypatch.setattr(m, "_remember_lines", lambda *a, **k: None)
    monkeypatch.setattr(m, "_cart_add", lambda ck, iid, sku, qty, **k: cart.__setitem__((iid, sku), qty) or f"c{iid}")
    monkeypatch.setattr(m, "_select", lambda ck, ids, only=False, selected=True, business="": calls.append((ids, only, business)) or ("sel", {}))
    first = m.combo_round(plan["id"])                         # round 1
    assert "Round 1 put in" in first and calls[-1][1:] == (True, "nn_mix")
    cart.clear()                                              # round 1 ordered: its lines are gone
    status = m.combo_round(plan["id"], add=False)
    assert "gone from the cart (ordered or removed)" in status and "Round 2: " in status and "not put in yet" in status
    second = m.combo_round(plan["id"])
    assert "Round 2 put in" in second
    assert "Nothing left" in m.combo_round(plan["id"])
    assert "No plan" in m.combo_round("plan-nope")
