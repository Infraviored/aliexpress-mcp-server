"""Kombi-Knaller (Bundle Deals) hits are flagged in the normal search; combo_only filters them."""
import json

import aliexpress_mcp_server as m


def _item(pid, combo):
    it = {"productId": pid, "title": {"displayTitle": f"JST XH {pid}"}, "prices": {"salePrice": {"minPrice": 1.84, "currencyCode": "EUR"}},
          "sellingPoints": [{"source": "npieces"}] if combo else [], "evaluation": {"starRating": 4.8}}
    if combo:
        it["rainbow"] = {"rainbowType": "channelProduct", "title": "Kombi-Knaller"}
    return it


def _html():
    return '<script>"itemList":' + json.dumps({"content": [_item("111", True), _item("222", False)]}, separators=(",", ":")) + "</script>"


def test_parser_flags_combo_hits():
    hits = m.parse_search_results(_html())
    assert {h["item_id"]: h["combo"] for h in hits} == {"111": True, "222": False}
    assert "111" in m.COMBO_ITEMS and "222" not in m.COMBO_ITEMS      # the planner learns Kombi items from searches


def test_search_marks_and_filters_combo(monkeypatch):
    monkeypatch.setattr(m, "_search", lambda *a, **k: m.parse_search_results(_html()))
    out = m.search_products("JST XH")
    assert "111" in out and "222" in out and out.count("Kombi (3+") == 1
    only = m.search_products("JST XH", combo_only=True)
    assert "item_id: 111" in only and "item_id: 222" not in only
