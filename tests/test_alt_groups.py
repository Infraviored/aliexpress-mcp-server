"""The decision page frames an offer together with the cart line it replaces, with reasons as pills."""
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
    p = page([card(title="Alternative", item_id="9", sku_id="8", replaces="111", default="yes", saving=0.6,
                   pills=[{"t": "spart 0,60 €", "k": "save"}]),
              card(title="Original", cart_id="111", default="no"),
              card(title="Andere Zeile", cart_id="222", default="yes")])
    grp = re.search(r'<div class="grp">.*?(?=<div class="row dec"[^>]*data-cart="222")', p, re.S).group(0)
    assert "Dasselbe Teil, 2 Wege" in grp
    assert grp.index('data-title="Original"') < grp.index('data-title="Alternative"')
    assert 'data-title="Andere Zeile"' not in grp
    assert '<span class="pill cart">jetzt im Korb</span>' in grp
    assert '<span class="pill save">spart 0,60 €</span>' in grp
    assert '<b class="save">−0,60 €</b>' in grp


def test_rows_carry_their_default_so_a_stale_saved_choice_is_ignored():
    p = page([card(cart_id="111", default="no"), card(item_id="9", replaces="111", default="yes")])
    assert 'data-def="no"' in p and 'data-def="yes"' in p
    assert "s.d === r.dataset.def" in p


def test_merge_offer_groups_all_replaced_lines():
    p = page([card(title="A", cart_id="1"), card(title="B", cart_id="2"),
              card(title="Neu", item_id="9", replaces=["1", "2"], default="yes")])
    assert "Ein Bedarf: 2 Zeilen oder eine" in p
    assert p.count('class="grp"') == 1


def test_no_group_without_replacement():
    p = page([card(cart_id="1"), card(cart_id="2", item_id="3")])
    assert 'class="grp"' not in p


def test_rows_carry_their_delivery_date_and_the_bar_shows_the_last_one():
    p = page([card(cart_id="1", until="2026-11-24T00:00"), card(cart_id="2", item_id="3")])
    assert 'data-until="2026-11-24"' in p and p.count("data-until=") == 1
    assert 'id="sb-dl"' in p and "Lieferung bis " in p
