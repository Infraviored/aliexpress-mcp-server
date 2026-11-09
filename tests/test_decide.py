import json

import pytest

import aliexpress_mcp_server as m
import report


def _card(**kw):
    c = {"title": "Leiste <b>weiss</b>", "item_id": "1005007076652944", "sku_id": "12000039323135462", "price": 20.39,
         "ship": 0, "choice": True, "img": "data:image/jpeg;base64,AAAA", "url": "https://www.aliexpress.com/item/1.html"}
    c.update(kw)
    return c


def _page(cards, **kw):
    return report.decide_page("tesla-led", "Tesla LED", "Frunk", [], [], [{"heading": "Frunk", "cards": cards}], "01.01.2026 10:00", **kw)


def test_page_has_a_row_with_choices_quantity_and_data():
    h = _page([_card(default="yes", fit="✓ Highland genannt")])
    assert 'class="row dec"' in h and 'data-price="20.39"' in h and 'data-sku="12000039323135462"' in h
    assert 'value="yes" checked' in h and 'type="number"' in h and "Highland genannt" in h
    assert "claudevoice:send" in h
    assert "claudevoice:sent" in h                      # the dashboard confirms the send
    assert "__NAME__" not in h and "__TH__" not in h and "__CS__" not in h


def test_titles_are_escaped_and_default_is_no():
    h = _page([_card()])
    assert "<b>weiss</b>" not in h and "&lt;b&gt;weiss&lt;/b&gt;" in h
    assert 'value="no" checked' in h


def test_page_needs_pictures_and_ids():
    with pytest.raises(ValueError, match="no picture"):
        _page([_card(img=None)])
    with pytest.raises(ValueError, match="item_id and sku_id"):
        _page([_card(sku_id="")])


def test_script_end_tag_in_name_cannot_break_out():
    h = report.decide_page("a</script><b>", "T", "", [], [], [{"heading": "x", "cards": [_card()]}], "s")
    assert "a</script><b>" not in h


def _msg(items):
    return "Auswahl X: ...\n```json\n" + json.dumps({"action": "select", "report": "x", "items": items}) + "\n```"


def test_apply_selection_adds_only_yes(monkeypatch):
    seen = []
    monkeypatch.setattr(m, "add_to_cart", lambda i, s, q, list_name="", need="", combo=False: (seen.append((i, s, q, need)), "ok")[1])
    out = m.apply_selection(_msg([
        {"item_id": "1", "sku_id": "2", "qty": 2, "state": "yes", "category": "Frunk"},
        {"item_id": "3", "sku_id": "4", "qty": 1, "state": "maybe", "category": "Frunk"},
        {"item_id": "5", "sku_id": "6", "qty": 1, "state": "no", "category": "Kofferraum"}]))
    assert seen == [("1", "2", 2, "Frunk")]
    assert "Not added" in out and "3/4" in out and "5/6" in out


def test_apply_selection_dry_run_changes_nothing(monkeypatch):
    monkeypatch.setattr(m, "add_to_cart", lambda *a, **k: pytest.fail("must not add"))
    out = m.apply_selection(_msg([{"item_id": "1", "sku_id": "2", "qty": 1, "state": "yes"}]), dry_run=True)
    assert "WOULD ADD" in out


def test_apply_selection_rejects_bad_input(monkeypatch):
    monkeypatch.setattr(m, "add_to_cart", lambda *a, **k: pytest.fail("must not add"))
    assert "Cannot read" in m.apply_selection("hallo")
    assert "Cannot read" in m.apply_selection('```json\n{"action":"other","items":[]}\n```')
    assert "Bad item" in m.apply_selection(_msg([{"item_id": "1; drop", "sku_id": "2", "state": "yes"}]))
    assert "Nothing chosen" in m.apply_selection(_msg([{"item_id": "1", "sku_id": "2", "state": "no"}]))


def test_apply_selection_reads_a_bare_json_message(monkeypatch):
    seen = []
    monkeypatch.setattr(m, "add_to_cart", lambda i, s, q, **k: (seen.append(i), "ok")[1])
    m.apply_selection(json.dumps({"action": "select", "items": [{"item_id": "7", "sku_id": "8", "state": "yes"}]}))
    assert seen == ["7"]


def test_cart_mode_has_delete_and_no_maybe_and_compact_details():
    h = _page([_card(cart_id="30016210959867", default="yes", tags=["Choice"], text="langer Titel")])
    assert 'value="delete"' in h and 'value="maybe"' in h and 'class="note-in"' in h
    assert 'data-cart="30016210959867"' in h and 'data-state0="yes"' in h and 'data-qty0="1"' in h
    assert 'class="more"' in h and "langer Titel" in h                    # chips and text only on tap (row.open)
    assert 'class="thumb"' in h and 'data-bulk="delete"' in h and 'data-all="1"' in h   # tap picture = mark, bulk actions


def test_row_shows_the_agents_opinion_rating_and_fit():
    h = _page([_card(verdict="Passt, aber 2 m reichen evtl. nicht", rating="4,8★ 208", fit="✓ Highland genannt")])
    assert 'class="vd"' in h and "Passt, aber 2 m reichen evtl. nicht" in h and "4,8★ 208" in h and 'class="ok"' in h
    assert "<h1>" not in h and 'class="dbar"' in h                       # no page header, one slim bar at the bottom


def test_decide_cart_passes_the_opinions_to_the_page(monkeypatch):
    line = {"cart_id": "11", "item_id": "1", "sku_id": "2", "title": "Title", "variant": "", "price": 5.0, "qty": 1, "checked": True,
            "valid": True, "ship": 0, "group": "global", "image": "", "delivery": None}
    monkeypatch.setattr(m, "load_cookies", lambda: {"a": "b"})
    monkeypatch.setattr(m, "_read_cart", lambda cookies, business="": {"lines": [line], "header": {}})
    monkeypatch.setattr(m, "_combo_lines", lambda cookies, cart: [])
    monkeypatch.setattr(m, "_line_store", lambda: {})
    monkeypatch.setattr(m, "_report_image", lambda card: "data:image/jpeg;base64,AAAA")
    written = {}
    monkeypatch.setattr(m, "_write_report", lambda name, page: (written.update(page=page), "ok")[1])
    m.decide_cart(opinions={"11": {"verdict": "Gute Wahl", "rating": "4,9★ 100"}}, check_deals=False)
    assert "Gute Wahl" in written["page"] and "4,9★ 100" in written["page"]


def test_decide_cart_offers_the_choice_alternative_of_a_global_line(monkeypatch):
    line = {"cart_id": "11", "item_id": "1", "sku_id": "2", "title": "Sensor", "variant": "Purple", "price": 14.59, "qty": 3,
            "checked": True, "valid": True, "ship": 3.38, "group": "global", "choice": False, "image": "", "delivery": ("16.11.", "24.11.")}
    alt = {"item_id": "99", "title": "Sensor Choice", "url": "https://x/99", "img": None, "buy": "3× Purple", "cost": 40.0, "days": 5,
           "sku_id": "77", "qty": 3, "ambiguous": False, "combo": False, "choice": True, "pieces": 3}
    monkeypatch.setattr(m, "load_cookies", lambda: {"a": "b"})
    monkeypatch.setattr(m, "_read_cart", lambda cookies, business="": {"lines": [line], "header": {}})
    monkeypatch.setattr(m, "_combo_lines", lambda cookies, cart: [])
    monkeypatch.setattr(m, "_line_store", lambda: {})
    monkeypatch.setattr(m, "_alt_cache", lambda: {})
    monkeypatch.setattr(m, "_line_deal", lambda ln, tag, cache: {"now": 60.0, "best": alt, "kombi": None})
    monkeypatch.setattr(m, "_report_image", lambda card: "data:image/jpeg;base64,AAAA")
    written = {}
    monkeypatch.setattr(m, "_write_report", lambda name, page: (written.update(page=page), "ok")[1])
    out = m.decide_cart()
    assert 'data-replaces="11"' not in written["page"] and "NOT ON THE PAGE YET" in out and "11 " in out   # not approved: held back
    out = m.decide_cart(approved_alternatives=["11"])
    p = written["page"]
    assert 'data-replaces="11"' in p and 'data-item="99"' in p and 'Alternative: ' in p and "billiger" in p
    assert "NOT ON THE PAGE YET" not in out


def test_decide_cart_rejects_an_implausibly_cheap_alternative(monkeypatch):
    line = {"cart_id": "11", "item_id": "1", "sku_id": "2", "title": "Hotend", "variant": "", "price": 20.3, "qty": 1,
            "checked": True, "valid": True, "ship": 2.66, "group": "global", "choice": False, "image": "", "delivery": None}
    alt = {"item_id": "99", "title": "Silicone sock", "url": "https://x/99", "img": None, "buy": "1× sock", "cost": 0.78, "days": 5,
           "sku_id": "77", "qty": 1, "ambiguous": False, "combo": False, "choice": False, "pieces": 1}
    monkeypatch.setattr(m, "load_cookies", lambda: {"a": "b"})
    monkeypatch.setattr(m, "_read_cart", lambda cookies, business="": {"lines": [line], "header": {}})
    monkeypatch.setattr(m, "_combo_lines", lambda cookies, cart: [])
    monkeypatch.setattr(m, "_line_store", lambda: {})
    monkeypatch.setattr(m, "_alt_cache", lambda: {})
    monkeypatch.setattr(m, "_line_deal", lambda ln, tag, cache: {"now": 60.0, "best": alt, "kombi": None})
    monkeypatch.setattr(m, "_report_image", lambda card: "data:image/jpeg;base64,AAAA")
    written = {}
    monkeypatch.setattr(m, "_write_report", lambda name, page: (written.update(page=page), "ok")[1])
    out = m.decide_cart(approved_alternatives=["11"])   # even approved: the price guard holds it back
    assert 'data-replaces' not in written["page"] and "BETTER DEALS" not in out   # a bait price never wins


def test_apply_selection_swaps_in_the_alternative_and_removes_the_old_line(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "add_to_cart", lambda *a, **k: (calls.append(("add", a)), "Added 3 x item")[1])
    monkeypatch.setattr(m, "remove_from_cart", lambda ids: (calls.append(("del", ids)), "Removed 1 item(s).")[1])
    m.apply_selection(_msg([{"item_id": "99", "sku_id": "77", "qty": 3, "state": "yes", "category": "Extras", "replaces": "11"}]))
    assert [c[0] for c in calls] == ["add", "del"] and calls[1][1] == ["11"]
    calls.clear()
    monkeypatch.setattr(m, "add_to_cart", lambda *a, **k: "Add to cart failed: boom")
    out = m.apply_selection(_msg([{"item_id": "99", "sku_id": "77", "qty": 3, "state": "yes", "category": "Extras", "replaces": "11"}]))
    assert calls == [] and "NOT removed" in out


def test_offer_mode_keeps_maybe_and_delete():
    h = _page([_card()])
    assert 'value="maybe"' in h and 'value="delete"' in h


def test_apply_selection_cart_lines(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "remove_from_cart", lambda ids: (calls.append(("del", ids)), "Removed 1 item(s).")[1])
    monkeypatch.setattr(m, "update_cart_quantity", lambda c, q: (calls.append(("qty", c, q)), "ok")[1])
    monkeypatch.setattr(m, "select_cart_items", lambda ids, only=False, selected=True: (calls.append(("sel", ids, selected)), "ok")[1])
    monkeypatch.setattr(m, "add_to_cart", lambda *a, **k: pytest.fail("a cart line is not added again"))
    monkeypatch.setattr(m, "_live_lines", lambda: {c: {"cart_id": c, "valid": True, "combo": False} for c in ("11", "12", "13")})
    m.apply_selection(_msg([
        {"item_id": "1", "sku_id": "2", "cart_id": "11", "qty": 1, "qty0": 1, "state": "delete"},
        {"item_id": "1", "sku_id": "3", "cart_id": "12", "qty": 3, "qty0": 1, "state": "yes"},
        {"item_id": "1", "sku_id": "4", "cart_id": "13", "qty": 1, "qty0": 1, "state": "no"}]))
    assert ("del", ["11"]) in calls and ("qty", "12", 3) in calls
    assert ("sel", ["13"], False) in calls and ("sel", ["12"], True) in calls


def test_apply_selection_cart_dry_run_and_bad_state(monkeypatch):
    monkeypatch.setattr(m, "remove_from_cart", lambda *a: pytest.fail("dry run"))
    out = m.apply_selection(_msg([{"item_id": "1", "sku_id": "2", "cart_id": "11", "qty": 1, "qty0": 1, "state": "delete"}]), dry_run=True)
    assert "WOULD DELETE 1" in out
    assert "Bad state" in m.apply_selection(_msg([{"item_id": "1", "sku_id": "2", "cart_id": "11", "state": "boom"}]))


def test_decide_cart_builds_a_page_from_the_cart(monkeypatch):
    line = {"cart_id": "11", "item_id": "1", "sku_id": "2", "title": "A long product title for the cart", "variant": "White",
            "price": 5.0, "qty": 2, "checked": True, "valid": True, "ship": 0, "group": "group_choice", "image": "",
            "delivery": ("13.11.", "18.11.")}
    monkeypatch.setattr(m, "load_cookies", lambda: {"a": "b"})
    monkeypatch.setattr(m, "_read_cart", lambda cookies, business="": {"lines": [line], "header": {}})
    monkeypatch.setattr(m, "_combo_lines", lambda cookies, cart: [])
    monkeypatch.setattr(m, "_line_store", lambda: {"11": {"list": "Extras", "need": "Duese"}})
    monkeypatch.setattr(m, "_report_image", lambda card: "data:image/jpeg;base64,AAAA")
    written = {}
    monkeypatch.setattr(m, "_write_report", lambda name, page: (written.update(name=name, page=page), "ok")[1])
    assert m.decide_cart(check_deals=False) == "ok"
    p = written["page"]
    assert written["name"] == "warenkorb.html" and 'data-cart="11"' in p and "Duese · White" in p and 'data-qty0="2"' in p
    assert 'data-cat="Extras"' in p and 'data-choice="1"' in p


def test_select_checks_every_line_and_names_the_ones_that_did_not_stick(monkeypatch):
    lines = [{"cart_id": str(i), "title": f"Teil {i}", "variant": "", "checked": False} for i in range(12)]
    monkeypatch.setattr(m, "_cart_pages", lambda cookies, business="": ({"data": {}}, []))
    monkeypatch.setattr(m.cartlib, "parse", lambda pages: {"lines": lines})
    sent = {}
    monkeypatch.setattr(m, "_cart_line_action",
                        lambda cookies, edits, merged=None, business="": (sent.update(edits), (list(edits), {}))[1])
    after = [dict(ln, checked=ln["cart_id"] != "11") for ln in lines]   # AliExpress left one unchecked
    monkeypatch.setattr(m, "_read_cart", lambda cookies, business="": {"lines": after, "summary": {}})
    text = m._select({"a": "b"}, [str(i) for i in range(12)])[0]
    assert len(sent) == 12                                   # no 10-line cap any more
    assert "NOT checked after the call (1): Teil 11" in text


def test_apply_selection_matches_the_live_cart(monkeypatch):
    """A Ja line gone from the cart is added again, an unavailable one is named, every Ja line is checked (no cap)."""
    calls = []
    monkeypatch.setattr(m, "select_cart_items", lambda ids, only=False, selected=True: (calls.append(("sel", ids, selected)), "ok")[1])
    monkeypatch.setattr(m, "add_to_cart", lambda *a, **k: (calls.append(("add", a, k)), "Added 2 x item")[1])
    live = {str(c): {"cart_id": str(c), "valid": True, "combo": False} for c in range(20, 32)}
    live["31"]["valid"] = False
    monkeypatch.setattr(m, "_live_lines", lambda: live)
    items = [{"item_id": "1", "sku_id": str(c), "cart_id": str(c), "qty": 1, "qty0": 1, "state": "yes",
              "category": "Extras", "title": f"Teil {c}"} for c in range(20, 32)]
    items.append({"item_id": "5", "sku_id": "6", "cart_id": "99", "qty": 2, "qty0": 2, "state": "yes",
                  "category": "Kombi-Knaller-Korb", "combo": True, "title": "Ladegerät"})
    out = m.apply_selection(_msg(items))
    assert out.startswith("Ergebnis:")
    assert ("add", ("5", "6", 2), {"need": "Kombi-Knaller-Korb", "combo": True}) in calls
    assert "Ladegerät: war nicht mehr im Korb, neu hinzugefügt." in out
    assert "Teil 31: nicht lieferbar" in out
    sel = [c for c in calls if c[0] == "sel"][0]
    assert len(sel[1]) == 12                     # all twelve Ja lines, no 10-line cap
    assert "Kassen-Link Extras (12 Zeilen)" in out


def test_page_sends_every_cart_line():
    h = _page([_card()])
    assert "v === r.dataset.state0" not in h     # no skipping of lines unchanged against the page's own state
    assert "it.title" in h



def test_kombi_lines_are_named_by_their_tab():
    import cart as c
    states = [{"data": {"x": {"cartLinesInfo": [{"hsCode": "8536909500", "itemId": "1", "skuId": 2}]}}}]
    hs = {}
    c._hs_codes(states, hs)
    assert hs == {("1", "2"): "8536909500"}
    assert c.tab_name("8536909500") == "Verbinder"
    lines = [{"hs": "8536909500", "qty": 5}, {"hs": "8536508000", "qty": 4}, {"qty": 1}]
    assert list(m._combo_tabs(lines)) == ["Verbinder", "Schalter", "Kategorie unbekannt"]
