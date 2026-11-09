"""Without variant pictures, get_variant_images shows the article's main photos."""
import aliexpress_mcp_server as m


def test_main_photos_when_variants_have_none(monkeypatch):
    offer = {"title": "ESP32S3 N16R8 Module", "images": ["https://x/a.jpg", "https://x/b.jpg"],
             "variants": [{"n": 1, "label": "1pc", "price": 5.59, "sku_id": "1", "salable": True}]}
    monkeypatch.setattr(m, "_pdp_offer", lambda item_id: offer)
    seen = []
    monkeypatch.setattr(m.deals, "contact_sheet", lambda rows, fetch, cur: (seen.append(rows), b"jpg" if rows and rows[0].get("image") else None)[1])
    out = m.get_variant_images.fn("1") if hasattr(m.get_variant_images, "fn") else m.get_variant_images("1")
    assert isinstance(out, list) and "main photos" in out[0]
    assert [r["image"] for r in seen[-1]] == ["https://x/a.jpg", "https://x/b.jpg"]
