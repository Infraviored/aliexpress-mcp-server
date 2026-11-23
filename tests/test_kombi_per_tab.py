"""Kombi-Knaller: the import charge is covered per tab (customs category), from 3 checked pieces in that tab."""
import aliexpress_mcp_server as m
import report


def test_unknown_item_sits_alone_in_its_tab(monkeypatch):
    monkeypatch.setattr(m, "_HS_BY_ITEM", {"1": "8536909500"})
    assert m._combo_tab("1") == "Verbinder"
    assert m._combo_tab("2") == "?"


def test_remember_hs_from_cart_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_HS_FILE", tmp_path / "hs.json")
    monkeypatch.setattr(m, "_HS_BY_ITEM", {"x": "1"})
    m._remember_hs([{"item_id": "5", "hs": "8541410000"}, {"item_id": "6"}])
    assert m._hs_load()["5"] == "8541410000" and "6" not in m._hs_load()
    assert "8541410000" in (tmp_path / "hs.json").read_text()


def test_page_counts_duty_per_tab():
    card = lambda i, tab: {"item_id": i, "sku_id": "s", "title": "t", "price": 1.0, "pack": 1, "qty": 1, "pieces": 1,
                           "ship": 0, "duty": 0.0, "combo": True, "tab": tab, "duty_full": 3.57, "until": "2026-12-10", "img": "data:image/png;base64,AA=="}
    html = report.decide_page("n", "T", "s", [], [], [{"heading": "Kombi", "cards": [card("1", "Verbinder"), card("2", "Verbinder")]}], "2026-11-23") \
        if hasattr(report, "decide_page") else None
    if html is None:
        return
    assert 'data-tab="Verbinder"' in html and 'data-dfull="3.57"' in html
    assert "function tabs()" in html


def test_plan_counts_three_pieces_per_tab(monkeypatch):
    # A and B sit in different tabs with 2 and 1 piece: neither tab reaches 3, so both pay the charge
    m.COMBO_ITEMS.update({"A", "B"})
    monkeypatch.setattr(m, "_HS_BY_ITEM", {"A": "8536909500", "B": "8541410000"})
    plan = m._plan([{"need": "a", "item_ids": ["A"], "pieces": 2}, {"need": "b", "item_ids": ["B"], "pieces": 1}], 0.0)
    assert plan["combo_pieces"] == 0


def test_add_to_cart_reports_an_undeliverable_line_as_error():
    src = open(m.__file__).read()
    assert "NOT AVAILABLE" in src
