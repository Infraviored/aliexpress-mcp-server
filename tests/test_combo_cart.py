"""The Kombi-Knaller cart (channel cart nn_mix): its blocks parse like the main cart's; add_to_cart(combo=True) targets it."""
import aliexpress_mcp_server as m
import cart as cartlib


def _block(cid, qty, price, valid=True, selected=True, comp="activity_cart_group_item"):
    return {"compName": comp, "fields": {
        "itemView": {"cartId": cid, "itemId": 1005007458395843, "skuId": "12000040839368395", "valid": valid,
                     "status": "VALID" if valid else "INVALID", "title": "5Sets JST XH", "sku": {"skuInfo": "3Pin/10CM"}},
        "priceViews": [{"amount": {"cent": price, "currencyCode": "EUR"}, "priceType": "showPrice"}],
        "quantityView": {"current": qty}, "checkbox": {"selected": selected}}}


def _state():
    return {"data": {
        "activity_cart_group_item_product_1": _block(1, 5, 215),
        "activity_cart_invalid_product_global_cart_2": _block(2, 1, 300, valid=False, selected=False, comp="activity_cart_invalid_product"),
        "activity_cart_group_item_page_furniture": {"compName": "activity_cart_group_item", "fields": {}},   # no cart line
    }}


def test_combo_blocks_parse_like_main_cart_lines():
    c = cartlib.parse([_state()])
    by = {ln["cart_id"]: ln for ln in c["lines"]}
    assert set(by) == {"1", "2"}                                   # the block without a cartId is dropped
    assert by["1"]["qty"] == 5 and by["1"]["price"] == 2.15 and by["1"]["checked"] and by["1"]["valid"]
    assert not by["2"]["valid"] and not by["2"]["checked"]
    assert cartlib.item_blocks(_state()) == {"1": "activity_cart_group_item_product_1",
                                             "2": "activity_cart_invalid_product_global_cart_2"}


def test_channel_payload_carries_business_only_for_the_combo_cart():
    assert "business" not in m._cart_base_payload()
    p = m._cart_base_payload(m.COMBO)
    assert p["business"] == "nn_mix" and '"sourceBizPage": "nnHsCode"' in p["bizParams"]


def test_combo_text_counts_checked_pieces(monkeypatch):
    cart = {"header": {"channels": [{"business": "nn_mix", "count": 2, "url": "https://combo"}]}}
    monkeypatch.setattr(m, "_read_cart", lambda ck, business="": cartlib.parse([_state()]))
    out = m._combo_text({}, cart)
    assert "1 valid line(s) of 2" in out and "5 pcs" in out and "import charge covered" in out and "INVALID" in out
    assert m._combo_text({}, {"header": {"channels": []}}) == ""


def test_view_cart_shows_combo_cart_when_main_cart_is_empty(monkeypatch):
    empty = {"lines": [], "header": {"count": 0, "channels": [{"business": "nn_mix", "count": 2, "url": "u"}]}}
    monkeypatch.setattr(m, "load_cookies", lambda: {"x": "y"})
    monkeypatch.setattr(m, "_read_cart", lambda ck, business="": cartlib.parse([_state()]) if business else empty)
    assert "Kombi-Knaller cart" in m.view_cart()


def test_add_to_cart_combo_posts_business_and_reports_the_line(monkeypatch):
    sent = {}
    monkeypatch.setattr(m, "load_cookies", lambda: {"x": "y"})
    monkeypatch.setattr(m, "_cart_pace", lambda: None)
    monkeypatch.setattr(m, "_read_cart", lambda ck, business="": cartlib.parse([_state()]))
    monkeypatch.setattr(m, "_remember_lines", lambda *a, **k: None)

    def fake(api, v, payload, **kw):
        sent.update(payload)
        return {"ret": ["SUCCESS::ok"], "data": {"cartId": "1"}}
    monkeypatch.setattr(m, "mtop_call", fake)
    out = m.add_to_cart("1005007458395843", "12000040839368395", 5, combo=True)
    assert sent["business"] == "nn_mix" and "Kombi-Knaller cart" in out and "cart_id 1" in out
