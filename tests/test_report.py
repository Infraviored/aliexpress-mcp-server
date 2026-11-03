import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import report  # noqa: E402

ITEM = {"img": "data:image/jpeg;base64,AAAA", "need": "Mic <pair>", "title": "ICS-43434 & co", "url": "https://x/1",
        "qty": 2, "variant": "1pcs", "cost": 19.98, "ship": 0.0, "kind": "Choice", "until": "31.10.",
        "ticked": False, "own": False, "extra": "", "slowest": True, "optional": False,
        "pieces": 2, "unit": 9.99}


def test_cart_page_escapes_and_shows_totals():
    page = report.cart_page([{"name": "Sat", "total": 159.92, "goods": 115.44, "ship": 7.01, "duty": 39.29,
                              "discount": 1.82, "latest": "15.11.", "checkout_url": "https://c?a=1&b=2",
                              "slowest": "Mic", "summary": "Nimm Choice.", "items": [ITEM],
                              "decisions": [{"kind": "choice", "title": "ESP über Choice", "price": (-1.78, 1.79),
                                             "date": ("15.11.", "04.11."), "facts": ["f"]}]}], [], "now")
    assert page.startswith("<title>Warenkorb Sat</title>")
    assert "159,92 €" in page and "Mic &lt;pair&gt;" in page and "ICS-43434 &amp; co" in page
    assert 'href="https://c?a=1&amp;b=2"' in page and "Zu entscheiden" in page
    assert "2 Stk · 9,99 €/Stk" in page
    assert "−1,78 € bis +1,79 €" in page and "<b>04.11.</b>" in page and "Nimm Choice." in page


def test_plan_page():
    o = {"img": None, "title": "T", "url": "https://x", "buy": "1× A", "net": 4.62, "unit": 4.62,
         "kind": "Choice", "days": "12 Tage", "diff": 0.0}
    page = report.plan_page("L", [{"need": "N", "pieces": 1, "why": None, "winner": o, "alts": [dict(o, diff=0.24)]}],
                            {"goods": 1.05, "ship": 0, "duty": 3.57, "saved": 0, "total": 4.62, "slowest": None}, "now")
    assert "+0,24 €" in page and "4,62 €" in page


def _card(**kw):
    return dict({"title": "Cover <1>", "img": "data:image/jpeg;base64,AAAA", "price": "3,15 €",
                 "tags": ["✓ im Korb", "Choice"], "url": "https://x/1?a=1&b=2"}, **kw)


def test_generic_page_layout_and_escaping():
    page = report.generic_page("T", "Sub", [{"label": "Summe", "value": "32,02 €"}],
                               [{"text": "Achtung <b>", "warn": True}],
                               [{"heading": "H", "cards": [_card(chosen=True)]}], "now")
    assert page.startswith("<title>T</title>")
    assert "32,02 €" in page and "Cover &lt;1&gt;" in page and "Achtung &lt;b&gt;" in page
    assert 'href="https://x/1?a=1&amp;b=2"' in page and 'class="note warn"' in page
    assert "row chosen" in page


def test_generic_page_refuses_cards_without_picture():
    import pytest
    with pytest.raises(ValueError, match="Cover without"):
        report.generic_page("T", "", [], [], [{"heading": "H", "cards": [_card(title="Cover without", img=None)]}], "now")


def test_generic_page_pick_mode_has_radios_codes_and_lightbox():
    sec = {"heading": "Teil", "pick": True, "cards": [_card(chosen=True), _card(title="Alt", text="anders")]}
    page = report.generic_page("T", "", [], [], [sec], "now")
    assert page.count('type="radio"') == 2 and "checked" in page
    assert ">A1<" in page and ">A2<" in page and 'id="pk-out"' in page and 'id="lb"' in page
    assert "clipboard" not in page


def test_generic_page_drops_non_http_links():
    page = report.generic_page("T", "", [], [], [{"heading": "H", "intro": "", "cards": [
        _card(title="A", url="javascript:alert(1)")]}], "x")
    assert "javascript:" not in page


def test_report_image_refuses_local_files_outside_report_dir(tmp_path, monkeypatch):
    import aliexpress_mcp_server as m
    monkeypatch.setattr(m, "REPORT_DIR", tmp_path / "reports")
    (tmp_path / "reports").mkdir()
    secret = tmp_path / "id_rsa.png"
    secret.write_bytes(b"x")
    ok = tmp_path / "reports" / "p.png"
    ok.write_bytes(b"\x89PNG")
    assert m._report_image({"image": str(secret)}) is None
    assert m._report_image({"image": str(ok)}).startswith("data:image/png;base64,")


def test_report_image_refuses_private_and_non_https_urls():
    import aliexpress_mcp_server as m
    for u in ("http://example.com/a.jpg", "https://127.0.0.1/a.jpg", "https://169.254.169.254/x.jpg",
              "https://localhost/a.jpg"):
        assert m._report_image({"image": u}) is None


def test_cdn_picture_url_keeps_sized_and_query_urls():
    import aliexpress_mcp_server as m
    assert m._cdn_picture_url("https://ae01.alicdn.com/kf/Sabc.jpg") == "https://ae01.alicdn.com/kf/Sabc.jpg_800x800.jpg"
    assert m._cdn_picture_url("https://ae01.alicdn.com/kf/Sabc.jpg_350x350.jpg") == "https://ae01.alicdn.com/kf/Sabc.jpg_350x350.jpg"
    assert m._cdn_picture_url("https://ae01.alicdn.com/kf/Sabc.jpg?w=1") == "https://ae01.alicdn.com/kf/Sabc.jpg?w=1"
