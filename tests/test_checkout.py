"""Checkout preview parsed from a real renderOrder response (2026-10-21, address and buyer removed)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import checkout  # noqa: E402

RESP = json.loads((Path(__file__).parent / "fixtures" / "checkout_render.json").read_text())


def test_totals_match_the_checkout_page():
    p = checkout.parse(RESP)
    assert (p["subtotal"], p["discounts"], p["shipping"]) == (112.55, 1.82, 7.01)
    assert (p["duty"], p["duty_off"], p["total"]) == (45.40, 6.12, 157.02)


def test_lines_with_shipping_and_store():
    p = checkout.parse(RESP)
    assert len(p["lines"]) == 13
    esp = next(ln for ln in p["lines"] if ln["cart_id"] == "30016002947345")
    assert (esp["qty"], esp["price"], esp["ship"], esp["choice"]) == (5, 4.99, 3.71, False)
    assert esp["store"].startswith("All-goods")
    assert sum(1 for ln in p["lines"] if ln["choice"]) == 11
    assert p["latest"] == "2026-11-15" and "Mini360" in p["slowest"]["title"]


def test_duty_choice_lines_are_capped():
    assert checkout.duty_for(9, 0) == 32.13
    assert checkout.duty_for(12, 0) == 32.13
    assert checkout.duty_for(12, 2) == 39.27


def test_money():
    assert checkout._money("1.234,56€") == 1234.56
    assert checkout._money("-1,82€") == -1.82
    assert checkout._money("Free shipping") == 0.0
