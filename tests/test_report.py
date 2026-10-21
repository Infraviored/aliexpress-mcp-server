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
