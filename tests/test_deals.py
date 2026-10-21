import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import deals  # noqa: E402

# TZT ESP32-S3 N16R8, 2026-10-20: 1PCS 4.67, 3PCS 13.99; shipping ≈ 3.30 + 0.10 per extra unit per line
ONE = {"sku_id": "1", "label": "Module / 1PCS", "price": 4.67, "pack": 1, "salable": True}
THREE = {"sku_id": "3", "label": "Module / 3PCS", "price": 13.99, "pack": 3, "salable": True}
QUOTE = {"A": 3.30, "B": 0.10, "free": False}


def test_shipping_is_charged_per_cart_line():
    ways = deals.best_ways([ONE, THREE], 5, QUOTE, choice_free=False, limit=50)
    mixed = next(w for w in ways if len(w["lines"]) == 2 and w["pieces"] == 5)
    assert mixed["line_ship"] == [3.30 + 0.10, 3.30] or sorted(mixed["line_ship"]) == [3.30, 3.40]
    assert abs(mixed["ship"] - 6.70) < 1e-9
    best = ways[0]
    assert len(best["lines"]) == 1  # one line beats two: 5×1PCS pays shipping once


def test_choice_ships_free_per_line():
    ways = deals.best_ways([ONE, THREE], 5, QUOTE, choice_free=True, limit=5)
    assert all(w["ship"] == 0 for w in ways)


def test_import_charges_count_per_line_not_per_piece():
    # Choice, free shipping: 1×3PCS + 2×1PCS (two lines) vs 5×1PCS (one line)
    ways = deals.best_ways([ONE, THREE], 5, QUOTE, choice_free=True, limit=50, duty=3.57)
    for w in ways:
        assert abs(w["duty"] - 3.57 * len(w["lines"])) < 1e-9
        assert abs(w["total"] - (w["goods"] + w["ship"] + w["duty"])) < 1e-9
    assert len(ways[0]["lines"]) == 1


DISC = {
    "tiers": [(18.25, 0.91), (31.94, 1.82)],
    "coupons": [
        {"off": 0.91, "min": 9.12, "how": "collected", "campaign_id": "a"},
        {"off": 0.91, "min": 13.69, "how": "follow & collect", "campaign_id": "b"},
        {"off": 2.74, "min": 80.30, "how": "collect", "campaign_id": "c"},
    ],
}


def test_discount_without_collecting_counts_collected_coupons_only():
    off, how = deals.discount_for(DISC, 85, coupons=False)
    assert round(off, 2) == 2.73 and "collected" in how
    assert round(deals.discount_for(DISC, 85)[0], 2) == 4.56


def test_coupon_to_collect():
    assert deals.coupon_to_collect(DISC, 23.35) is None          # the collected one is as good
    assert deals.coupon_to_collect(DISC, 85)["campaign_id"] == "c"
    assert deals.coupon_to_collect({"tiers": [], "coupons": [DISC["coupons"][1]]}, 20) is None  # never follow


def _panel(*promos):
    import json
    return {"data": {"result": {"panelPageGroupResultStr": json.dumps(
        {"panelPageGroupInfoList": [{"panelPagePromotionList": list(promos)}]})}}}


def _coupon(**kw):
    p = {"displayType": "shopCoupon", "campaignId": 1, "sellerId": 9,
         "resourcePositions": [{"key": "title", "tipList": ["0,91€ off"]},
                               {"key": "sub_title", "tipList": ["orders over 13,69€"]}]}
    p.update(kw)
    return p


def test_follow_coupons_are_recognised_in_any_language():
    d = deals.parse_discounts(_panel(_coupon(collectText="Folgen & sammeln", bizType=121)))
    assert d["coupons"][0]["how"] == "follow & collect"
    d = deals.parse_discounts(_panel(_coupon(collectText="Suivre", couponAssignParam={"sellerCouponActivityType": 21})))
    assert d["coupons"][0]["how"] == "follow & collect"
    d = deals.parse_discounts(_panel(_coupon(collectText="Sammeln", bizType=100)))
    assert d["coupons"][0]["how"] == "collect"
