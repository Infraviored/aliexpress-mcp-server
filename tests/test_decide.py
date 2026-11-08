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
    assert "claudevoice:send" in h and "Schätzung" in h
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
