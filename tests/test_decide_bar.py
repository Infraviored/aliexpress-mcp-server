"""The bottom bar of the decision page: the cart button is always there, and the totals use the rows' basis."""
import report


def _page():
    card = {"title": "ESP32-S3 · N16R8", "item_id": "1", "sku_id": "2", "cart_id": "3", "price": 7.49, "qty": 2, "img": "data:image/gif;base64,R0lGODlhAQABAAAAACw=",
            "default": "yes", "parts": {"goods": 14.98, "ship": 1.5, "duty": 0.4, "save": 0}}
    return report.decide_page("w", "Warenkorb", "", [], [], [{"heading": "Teile", "cards": [card]}], "s")


def test_cart_button_is_never_hidden():
    html = _page()
    assert 'id="sb-cart" class="send cart" disabled>' in html
    assert 'id="sb-cart" class="send cart" hidden' not in html
    assert 'id="sb-note"' in html and "Korb-Server" in html     # says why it is grey
    assert "j.busy" in html                                      # a running cart run is shown, not hidden


def test_bar_and_section_totals_use_the_line_totals():
    html = _page()
    assert "g += lineTotal(r)" in html
    assert "dataset.price) * q" not in html
    assert 'data-s="1.50" data-d="0.40"' in html
