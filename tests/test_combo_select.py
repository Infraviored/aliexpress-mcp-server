"""Kombi-Knaller lines can be checked, unchecked, resized and removed by cart_id, like main cart lines."""
import aliexpress_mcp_server as m
import cart as cartlib
from tests.test_combo_cart import _state


def _world(monkeypatch, main_ids=("9",)):
    calls = []
    monkeypatch.setattr(m, "load_cookies", lambda: {"x": "y"})
    monkeypatch.setattr(m, "_remember_lines", lambda *a, **k: None)

    def pages(ck, business=""):
        st = _state() if business else {"data": {}}
        return st, [st]
    monkeypatch.setattr(m, "_cart_pages", pages)
    monkeypatch.setattr(m, "_read_cart", lambda ck, business="": cartlib.parse([_state()]) if business else
                        {"lines": [{"cart_id": c, "checked": True, "valid": True, "qty": 1, "price": 1.0} for c in main_ids],
                         "summary": {}, "header": {}})

    def action(ck, edits, merged=None, business=""):
        calls.append((business, sorted(edits)))
        if not business:
            return [c for c in edits if c in main_ids], {c: "not found in cart" for c in edits if c not in main_ids}
        return list(edits), {}
    monkeypatch.setattr(m, "_cart_line_action", action)
    return calls


def test_select_falls_back_to_the_combo_cart_for_unknown_ids(monkeypatch):
    calls = _world(monkeypatch)
    out = m.select_cart_items(["2"], only=False, selected=True)
    assert "Kombi-Knaller cart" in out and "line(s) checked" in out
    assert ("nn_mix", ["2"]) in calls


def test_unchecking_a_combo_line(monkeypatch):
    calls = _world(monkeypatch)
    out = m.select_cart_items(["1"], selected=False)
    assert ("nn_mix", ["1"]) in calls and "Unchecked 1" in out


def test_remove_and_resize_try_the_combo_cart_when_the_main_cart_lacks_the_line(monkeypatch):
    calls = _world(monkeypatch)
    assert "Removed 1 item" in m.remove_from_cart(["1"])
    assert "quantity now 4" in m.update_cart_quantity("1", 4)
    assert calls.count(("nn_mix", ["1"])) == 2
