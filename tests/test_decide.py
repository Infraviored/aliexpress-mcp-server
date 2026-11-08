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
    assert 'value="delete"' in h and 'value="maybe"' not in h
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
    m.decide_cart(opinions={"11": {"verdict": "Gute Wahl", "rating": "4,9★ 100"}})
    assert "Gute Wahl" in written["page"] and "4,9★ 100" in written["page"]


def test_offer_mode_keeps_maybe_and_delete():
    h = _page([_card()])
    assert 'value="maybe"' in h and 'value="delete"' in h


def test_apply_selection_cart_lines(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "remove_from_cart", lambda ids: (calls.append(("del", ids)), "Removed 1 item(s).")[1])
    monkeypatch.setattr(m, "update_cart_quantity", lambda c, q: (calls.append(("qty", c, q)), "ok")[1])
    monkeypatch.setattr(m, "select_cart_items", lambda ids, only=False, selected=True: (calls.append(("sel", ids, selected)), "ok")[1])
    monkeypatch.setattr(m, "add_to_cart", lambda *a, **k: pytest.fail("a cart line is not added again"))
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
    assert m.decide_cart() == "ok"
    p = written["page"]
    assert written["name"] == "warenkorb.html" and 'data-cart="11"' in p and "Duese · White" in p and 'data-qty0="2"' in p
    assert 'data-cat="Extras"' in p and 'data-choice="1"' in p
