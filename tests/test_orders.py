import json

import orders


def _order(oid, status, code, date="21. Okt 2026"):
    return {"fields": {
        "orderId": oid, "statusText": status, "utParams": {"args": {"orderStatus": code}},
        "orderDateText": date, "formatPriceInfo": "13,53€|13|53", "currencyCode": "EUR",
        "storeName": "Shop", "storePageUrl": "//www.aliexpress.com/store/1", "orderDetailUrl": "https://x/d?orderId=" + oid,
        "orderLines": [{"productId": 1005, "skuId": 12, "itemTitle": "Ring", "quantity": 4,
                        "formatPriceInfo": "2,49€|2|49", "itemImgUrl": "https://i/x.jpg_220x220.jpg",
                        "itemDetailUrl": "//www.aliexpress.com/item/1005.html", "itemExtraInfo": "<b>Gutschein</b>",
                        "skuAttrs": [{"name": "Color", "text": "24 bit"}]}]}}


def _resp(has_more=True):
    return {"data": {
        "data": {
            "pc_om_list_order_1": _order("1", "Abgeschlossen", 9),
            "pc_om_list_order_2": _order("2", "Zu zahlen", 1, "Oct 3, 2026"),
            "pc_om_list_body_9": {"fields": {"hasMore": has_more, "pageIndex": 1, "pageSize": 10}, "id": "9"},
            "pc_om_list_header_action_8": {"fields": {"statusTab": "all", "timeOption": "all"}, "id": "8"},
        },
        "linkage": {"common": {"a": 1}, "input": ["x"], "request": ["x"], "signature": "s", "extra": 1},
        "hierarchy": {"structure": {"root": []}, "component": ["x"]},
        "endpoint": {"mode": "pc"}}}


def test_status_words_do_not_collide():
    assert orders.status_of("Abgeschlossen") == "abgeschlossen"      # not "geschlossen" = storniert
    assert orders.status_of("Geschlossen") == "storniert"
    assert orders.status_of("Zu zahlen") == "wartet_auf_zahlung"
    assert orders.status_of("Wird verarbeitet") == "in_bearbeitung"
    assert orders.status_of("Rückerstattung abgeschlossen") == "rueckerstattung"
    assert orders.status_of("Delivered") == "zugestellt"
    assert orders.status_of("", 9) == "abgeschlossen"
    assert orders.status_of("???") == "unbekannt"


def test_dates_and_prices():
    assert orders.iso_date("21. Okt 2026") == "2026-10-21"
    assert orders.iso_date("4. März 2026") == "2026-03-04"
    assert orders.iso_date("Oct 3, 2026") == "2026-10-03"
    assert orders.iso_date("nix") == ""
    assert orders.price("13,53€|13|53") == 13.53
    assert orders.price("") is None


def test_parse_list():
    found, state = orders.parse_list(_resp())
    assert [o["order_id"] for o in found] == ["1", "2"]
    o = found[0]
    assert (o["status"], o["date"], o["total"], o["store"]) == ("abgeschlossen", "2026-10-21", 13.53, "Shop")
    it = o["items"][0]
    assert (it["item_id"], it["sku_id"], it["quantity"], it["unit_price"]) == ("1005", "12", 4, 2.49)
    assert it["variant"] == "Color: 24 bit" and it["note"] == "Gutschein"
    assert it["url"] == "https://www.aliexpress.com/item/1005.html"
    assert found[1]["status"] == "wartet_auf_zahlung" and found[1]["date"] == "2026-10-03"
    assert state["has_more"] and state["body_key"] == "pc_om_list_body_9"


def test_next_payload_sends_state_back():
    _, state = orders.parse_list(_resp())
    payload = orders.next_payload(state, "shipped")
    params = json.loads(payload["params"])
    data = json.loads(params["data"])
    assert data["pc_om_list_body_9"]["fields"]["pageIndex"] == 2
    assert data["pc_om_list_header_action_8"]["fields"]["statusTab"] == "shipped"
    assert params["operator"] == "pc_om_list_body_9"
    assert set(json.loads(params["linkage"])) == {"common", "input", "request", "signature"}
    assert json.loads(params["hierarchy"]) == {"structure": {"root": []}}
    assert state["blocks"]["pc_om_list_body_9"]["fields"]["pageIndex"] == 1   # the snapshot is untouched
    assert orders.init_payload("all")["statusTab"] is None and orders.init_payload("unpaid")["statusTab"] == "unpaid"


def test_parse_detail_has_no_address():
    resp = {"data": {"data": {
        "detail_simple_order_info_component_1": {"fields": {
            "payTime": "21. Okt 2026", "orderShipTime": "22. Okt 2026", "orderEndTime": "4. Nov 2026",
            "addressVO": {"contactName": "Someone", "detailAddress": "Street 1"}}},
        "detail_order_price_block_2": {"fields": {"priceDetails": [
            {"title": "Versand", "value": "Kostenloser Versand"}, {"title": "Geschätzte Einfuhrabgaben", "value": "3,57€"}]}},
        "detail_product_block_3": {"fields": {"productVOList": [
            {"etaAndTrack": {"expectDeliveryDateTitle": "Voraussichtliches Lieferdatum: 5. Nov 2026",
                             "trackingNumber": "LP00123456789"}}]}}}}}
    d = orders.parse_detail(resp)
    assert (d["eta"], d["eta_iso"], d["paid"], d["shipped"], d["finished"]) == (
        "5. Nov 2026", "2026-11-05", "2026-10-21", "2026-10-22", "2026-11-04")
    assert d["shipping"] == "Kostenloser Versand" and d["import_charges"] == "3,57€"
    assert d["tracking"] == ["LP00123456789"]
    assert "Street" not in json.dumps(d) and "Someone" not in json.dumps(d)
