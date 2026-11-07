import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import report  # noqa: E402

ITEM = {"img": "data:image/jpeg;base64,AAAA", "need": "Mic <pair>", "title": "ICS-43434 & co", "url": "https://x/1",
        "qty": 2, "variant": "1pcs", "cost": 19.98, "ship": 0.0, "kind": "Choice", "until": "31.10.",
        "ticked": False, "own": False, "extra": "", "slowest": True, "optional": False,
        "pieces": 2, "unit": 9.99}


def _page(**kw):
    it = dict(ITEM, kind="Choice", cost=10.0, slowest=False, list="")
    base = {"name": "Sat", "total": 13.57, "goods": 10.0, "ship": 0.0, "duty": 3.57, "discount": 0.0, "mismatch": 0.0,
            "latest": "04.12.", "slowest": "Mic", "checkout_url": "https://c?a=1&b=2", "checkout_all_url": "https://all",
            "items": [it], "decisions": [], "unlisted": [], "summary": ""}
    return dict(base, **kw)


def test_cart_page_escapes_shows_total_and_one_checkout_button():
    page = report.cart_page(_page(), "now")
    assert page.startswith("<title>Warenkorb Sat</title>")
    assert "13,57 €" in page and "Mic &lt;pair&gt;" in page and "ICS-43434 &amp; co" in page
    assert 'href="https://c?a=1&amp;b=2"' in page and page.count('class="btn"') == 1
    assert "Alles zur Kasse (1 Teil)" in page and "2 Stk · 9,99 €/Stk" in page
    assert page.count("<h1>") == 1 and "purple" not in page


def test_cart_page_groups_by_channel_and_shows_group_sums():
    items = [dict(ITEM, kind="Choice", cost=10.0, slowest=False, list="A"),
             dict(ITEM, kind="global", cost=5.5, slowest=False, list="B")]
    page = report.cart_page(_page(items=items, total=20.0), "now")
    assert "Globale Händler" in page and ">Choice<" in page
    assert "10,00 €" in page and "5,50 €" in page and "20,00 €" in page
    assert 'class="chip">A<' in page and "auf 2 Listen" not in page


def test_cart_page_unlisted_group_has_own_link_and_is_not_in_total():
    page = report.cart_page(_page(unlisted=[{"img": None, "title": "Fan", "url": "https://x/f", "qty": 1, "cost": 42.59}]), "now")
    assert "Ohne Liste" in page and "nicht im Gesamtpreis" in page and "42,59 €" in page
    assert 'href="https://all"' in page and "Kasse mit allen 2 Zeilen" in page


def test_cart_page_decision_vs_note_and_mismatch():
    real = {"kind": "choice", "title": "ESP über Choice", "price": (-1.0, 1.0), "date": None, "facts": []}
    note = {"kind": "agent", "title": "Nicht nehmen", "price": (0, 0), "date": None, "facts": ["passt nicht"]}
    page = report.cart_page(_page(decisions=[real, note], mismatch=1.25), "now")
    assert "Zu entscheiden" in page and "1. ESP über Choice" in page
    assert page.count("<article") == 1                          # the note is no card
    assert "Nicht nehmen passt nicht" in page and "weichen um 1,25 €" in page


def test_cart_page_survives_odd_decisions_and_blocks_javascript_links():
    odd = {"kind": "agent", "title": None, "price": 2.5, "date": None, "facts": [None, "x"],
           "link": "javascript:alert(1)", "link_text": "L"}
    page = report.cart_page(_page(decisions=[odd, {"title": None, "facts": [None]}],
                                  items=[dict(ITEM, kind="Choice", cost=1.0, url="javascript:evil()")]), "now")
    assert "javascript:" not in page and "1. " in page


def test_cart_page_edge_cases_no_items_no_total_odd_kind_and_title():
    only_unlisted = report.cart_page(_page(items=[], total=None, checkout_url="",
                                           unlisted=[{"img": None, "title": None, "url": "https://x", "qty": 1, "cost": 1.0}]), "now")
    assert "Alles zur Kasse (0" not in only_unlisted and "Kasse mit allen 1 Zeilen" in only_unlisted
    assert 'class="btn ghost"' not in only_unlisted and "Einfuhrabgabe fehlt" in only_unlisted
    odd = report.cart_page(_page(items=[dict(ITEM, kind="Sonstige", cost=2.0, slowest=False, list="")]), "now")
    assert "Globale Händler" in odd and "2,00 €" in odd


def test_decision_helper_and_plurals():
    assert report._is_decision({"price": 2.5}) and not report._is_decision({"price": (0, 0)})
    assert report._teile(1) == "1 Teil" and report._teile(3) == "3 Teile"


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
