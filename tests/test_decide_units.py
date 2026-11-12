"""decide_report: pack size and need give the quantity, the pieces per row and the price per piece."""
import aliexpress_mcp_server as m


def test_pack_and_need(monkeypatch):
    monkeypatch.setattr(m, "_report_image", lambda c: "data:image/png;base64,AA==")
    out = {}
    monkeypatch.setattr(m, "_write_report", lambda name, page: out.setdefault("page", page) and name)
    cats = [{"heading": "Sockel", "need": 6, "cards": [
        {"title": "DIP-14, 10 Stück", "item_id": "1", "sku_id": "2", "price": 1.59, "pack": 10, "default": "yes"},
        {"title": "DIP-14, 4 Stück", "item_id": "3", "sku_id": "4", "price": 0.80, "pack": 4}]}]
    m.decide_report("t", "Teile", cats)
    page = out["page"]
    a, b = cats[0]["cards"]
    assert a["qty"] == 1 and a["pieces"] == 10                  # one 10-pack covers 6
    assert b["qty"] == 2 and b["pieces"] == 8                   # two 4-packs needed
    assert 'data-need="6"' in page and 'class="ch global"' in page and "10 Stück</b> für Bedarf 6" in page
    assert "0,16 €/Stück" in page                         # 1,59 € / 10 pieces
    assert not hasattr(m, "make_report")
