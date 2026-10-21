"""Cart parsing against a real two-page cart (2026-10-20, ids anonymised)."""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cart  # noqa: E402

PAGES = json.loads((Path(__file__).parent / "fixtures" / "cart_pages.json").read_text())


def _cart():
    return cart.parse(PAGES)


def test_lines_groups_and_totals_match_the_web_page():
    c = _cart()
    assert len(c["lines"]) == 20
    assert {ln["group"] for ln in c["lines"]} == {"group_choice", "global"}
    assert sum(1 for ln in c["lines"] if ln["group"] == "global") == 5
    assert all(ln["currency"] == "EUR" for ln in c["lines"])
    s = c["summary"]
    assert (s["subtotal"], s["shipping"], s["total"]) == (88.40, 13.28, 100.77)
    assert [amt for _, amt in s["discounts"]] == [0.91]  # page 2 came from a German browser
    assert sum(ln["ship"] for ln in c["lines"] if ln["checked"]) == 13.28


def test_line_fields():
    esp = next(ln for ln in _cart()["lines"] if ln["cart_id"] == "30016002947345")
    assert esp["qty"] == 2 and esp["price"] == 4.99 and esp["ship"] == 3.67
    assert esp["variant"] == "Module/1PCS" and esp["checked"]
    assert esp["delivery"] == ("2026-11-03", "2026-11-11")
    assert esp["group"] == "global"


def test_channel_carts():
    ch = _cart()["header"]["channels"]
    assert ch and ch[0]["business"] == "nn_mix" and ch[0]["count"] == 18


def test_shipping_report_explains_paid_lines():
    r = cart.shipping_report(_cart())
    reasons = {ln["cart_id"]: why for ln, why in r["paid"]}
    assert len(reasons) == 4
    assert "paid twice" in reasons["30016071703458"]           # second ESP line
    assert "more than the goods" in reasons["30016070975831"]  # Mini-360: 3.30 for 1.39
    assert r["latest"] == "2026-11-15"


def test_format_has_checkout_link_for_checked_lines():
    text = cart.format_cart(_cart())
    url = next(w for w in text.split() if w.startswith("https://www.aliexpress.com/p/trade/confirm.html"))
    ids = parse_qs(urlparse(url).query)["availableProductShopcartIds"][0].split(",")
    assert len(ids) == 10 and "30016002947345" in ids


def test_merge_appends_structure_and_unions_triggers():
    a = {"data": {"x": 1}, "page": {"root": "r", "structure": {"c": ["p1"]}},
         "asyncHandler": {"handlerParams": {"q": "old"}, "triggers": ["t1"]}}
    b = {"data": {"y": 2}, "page": {"root": "r", "structure": {"c": ["p2"], "d": ["p3"]}},
         "asyncHandler": {"handlerParams": {"q": "new"}, "triggers": ["t2"]}}
    m = cart.merge(cart.merge(None, a), b)
    assert m["page"]["structure"] == {"c": ["p1", "p2"], "d": ["p3"]}
    assert m["asyncHandler"]["handlerParams"] == {"q": "new"}
    assert m["asyncHandler"]["triggers"] == ["t1", "t2"]
    assert m["data"] == {"x": 1, "y": 2}
    assert a["page"]["structure"] == {"c": ["p1"]}  # inputs untouched


def test_item_blocks_and_pagination():
    assert len(cart.item_blocks(PAGES[0])) == 10
    assert cart.has_more(PAGES[0]) and not cart.has_more(PAGES[1])


def test_merge_moves_a_line_instead_of_listing_it_twice():
    a = {"data": {}, "page": {"structure": {"global": ["p1", "p2"], "choice": ["p3"]}}, "asyncHandler": {}}
    b = {"data": {}, "page": {"structure": {"choice": ["p2"]}}, "asyncHandler": {}}
    m = cart.merge(cart.merge(None, a), b)
    assert m["page"]["structure"] == {"global": ["p1"], "choice": ["p3", "p2"]}


def _make_line(cart_id, seller_id, item_id, ship, group="global", checked=True):
    return {
        "cart_id": cart_id, "item_id": item_id, "sku_id": "1",
        "variant": "default", "title": f"item-{cart_id}", "valid": True,
        "checked": checked, "price": 5.0, "currency": "EUR",
        "qty": 1, "max_qty": 10, "store": f"store-{seller_id}",
        "seller_id": seller_id, "ship": ship,
        "ship_service": None, "ship_from": None,
        "delivery": (None, None), "days": (None, None), "group": group,
    }


def test_shipping_report_free_then_paid_same_store_warns_paid_twice():
    """A free-shipping line followed by a paid line from the same store must
    trigger the 'paid twice' note on the paid line (regression for F1: the old
    code skipped free lines in seen_store, so the paid line never matched)."""
    free_line = _make_line("cid-1", seller_id="s99", item_id="i1", ship=0.0)
    paid_line = _make_line("cid-2", seller_id="s99", item_id="i2", ship=3.50)
    c = {"lines": [free_line, paid_line], "groups": {}, "header": {}, "summary": {}}
    r = cart.shipping_report(c)
    reasons = {ln["cart_id"]: why for ln, why in r["paid"]}
    assert "cid-1" not in reasons, "free line must not appear in paid"
    assert "cid-2" in reasons, "paid line must appear in paid"
    assert "paid twice" in reasons["cid-2"], (
        f"Expected 'paid twice' in reason for paid line, got: {reasons['cid-2']!r}"
    )



def test_format_lists_separates_list_from_other_lines_and_flags_leftovers():
    c = _cart()
    ids = [ln["cart_id"] for ln in c["lines"]][:12]
    tags = {cid: {"list": "sat", "need": f"need {i}"} for i, cid in enumerate(ids)}
    text = cart.format_cart(c, tags=tags)
    assert "## List 'sat' — 12 line(s)" in text
    assert "## Other lines (not on a list)" in text
    assert "the cart page keeps only 10 ticked" in text or "ticked on the cart page" in text
    link = next(l for l in text.splitlines() if "Checkout link for the whole list" in l)
    assert parse_qs(urlparse(link.split(": ", 1)[1]).query)["availableProductShopcartIds"][0].split(",") == ids


def test_models_match_same_part_names():
    assert cart._models("Interface I2S PCM5102A DAC Decoder GY-PCM5102") >= {"PCM5102A"}
    assert "MP1584EN" in cart._models("5/10pcs MP1584EN Adjustable 3A")
    assert not cart._models("2cm Self Adhesive Soundproofing")
