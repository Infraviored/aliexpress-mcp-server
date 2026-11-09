"""The decision page frames an offer together with the cart line it replaces and compares both on one price basis."""
import re

import report

PX = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=="


def card(**k):
    c = {"title": "Mikrofon", "item_id": "1", "sku_id": "2", "price": 6.59, "img": PX, "qty": 2}
    c.update(k)
    return c


def page(cards):
    return report.decide_page("t", "Warenkorb", "", [], [], [{"heading": "Sat", "cards": cards}], "s")


def test_offer_and_replaced_line_share_one_group_original_first():
    p = page([card(title="Alternative", item_id="9", sku_id="8", replaces="111", default="yes", price=7.62, qty=2,
                   until="2026-11-16", seller="Choice", pieces=10),
              card(title="Original", cart_id="111", default="no", qty=5, until="2026-11-09", seller="Shop X", pieces=5,
                   parts={"goods": 13.45, "ship": 2.21, "duty": 0.44, "save": 0}),
              card(title="Andere Zeile", cart_id="222", default="yes")])
    grp = re.search(r'<div class="grp">.*?(?=<div class="row dec"[^>]*data-cart="222")', p, re.S).group(0)
    assert "Alternative: 0,86 € billiger, aber Lieferung 16.11. statt 09.11." in grp
    assert grp.index('data-title="Original"') < grp.index('data-title="Alternative"')
    assert 'data-title="Andere Zeile"' not in grp
    assert ">16,10 €<" in grp and ">15,24 €<" in grp      # the same basis: goods + shipping + import
    assert "3,22 €/Stück" in grp and "1,52 €/Stück" in grp
    assert "Lieferung bis <b>09.11.</b> · Shop X · jetzt im Korb" in grp
    assert 'class="pill' not in grp


def test_rows_carry_their_default_so_a_stale_saved_choice_is_ignored():
    p = page([card(cart_id="111", default="no"), card(item_id="9", replaces="111", default="yes")])
    assert 'data-def="no"' in p and 'data-def="yes"' in p
    assert "s.d === r.dataset.def" in p


def test_merge_offer_groups_all_replaced_lines():
    p = page([card(title="A", cart_id="1"), card(title="B", cart_id="2"),
              card(title="Neu", item_id="9", replaces=["1", "2"], default="yes")])
    assert "Eine Zeile statt 2:" in p
    assert p.count('class="grp"') == 1


def test_no_group_without_replacement():
    p = page([card(cart_id="1"), card(cart_id="2", item_id="3")])
    assert 'class="grp"' not in p


def test_rows_carry_their_delivery_date_and_the_bar_shows_the_last_one():
    p = page([card(cart_id="1", until="2026-11-24T00:00"), card(cart_id="2", item_id="3")])
    assert 'data-until="2026-11-24"' in p and p.count("data-until=") == 1
    assert 'id="sb-dl"' in p and "Lieferung bis " in p
