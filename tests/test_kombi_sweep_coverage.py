"""
Unit tests for Kombi sweeping, candidate ranking, coverage reporting,
server-computed decide rows, and apply_selection tagging (Requirements A-E).
"""
import json
import time
import pytest

import aliexpress_mcp_server as m
import cart as cartlib
import report


# ==============================================================================
# Requirement A: Kombi sweep and disk persistence
# ==============================================================================

def test_combo_disk_persistence(tmp_path, monkeypatch):
    """Kombi items are saved to disk with TTL and loaded on startup."""
    combo_file = tmp_path / "combo-cache.json"
    monkeypatch.setattr(m, "_COMBO_FILE", combo_file)

    # Empty file initially
    assert m._combo_disk_load() == {}

    # Store items
    m._remember_combo("1001")
    m._remember_combo("1002")

    loaded = m._combo_disk_load()
    assert "1001" in loaded and "1002" in loaded
    assert m._is_combo("1001") is True
    assert m._is_combo("9999") is False

    # Simulate expired item
    data = {
        "1001": time.time() - (m.COMBO_DISK_TTL + 100),  # expired
        "1002": time.time(),                              # valid
    }
    combo_file.write_text(json.dumps(data))
    reloaded = m._combo_disk_load()
    assert "1001" not in reloaded
    assert "1002" in reloaded


def test_parse_search_cards_flags_and_remembers_combo(tmp_path, monkeypatch):
    """parse_search_results flags combo items and persists them."""
    monkeypatch.setattr(m, "_COMBO_FILE", tmp_path / "combo-cache.json")

    def _item(pid, combo):
        it = {"productId": pid, "title": {"displayTitle": f"Widget {pid}"},
              "prices": {"salePrice": {"minPrice": 1.84, "currencyCode": "EUR"}},
              "sellingPoints": [{"source": "npieces"}] if combo else [], "evaluation": {"starRating": 4.8}}
        if combo:
            it["rainbow"] = {"rainbowType": "channelProduct", "title": "Kombi-Knaller"}
        return it

    html = '<script>"itemList":' + json.dumps({"content": [_item("item_c1", True), _item("item_reg", False)]}, separators=(",", ":")) + "</script>"
    hits = m.parse_search_results(html)
    assert len(hits) == 2
    assert hits[0]["combo"] is True
    assert hits[1]["combo"] is False
    assert m._is_combo("item_c1") is True
    assert m._is_combo("item_reg") is False


def test_sweep_search_for_need_merges_channels_and_dedupes(monkeypatch):
    """_sweep_search_for_need searches normal and Kombi channels and dedupes hits."""
    def fake_search(query, sort_by="default", page=1, choice_only=False):
        if sort_by == "orders" and page == 1:
            return [
                {"item_id": "1", "title": "Item 1", "combo": False, "orders": 500},
                {"item_id": "3", "title": "Item 3 Kombi", "combo": True, "orders": 300},
            ]
        elif page == 2 and sort_by in ("best_match", "default"):
            return [
                {"item_id": "4", "title": "Item 4", "combo": False, "orders": 10},
            ]
        elif page == 1:
            return [
                {"item_id": "1", "title": "Item 1", "combo": False, "orders": 500},
                {"item_id": "2", "title": "Item 2 Choice", "combo": False, "choice": True, "orders": 100},
            ]
        return []

    monkeypatch.setattr(m, "_search", fake_search)
    monkeypatch.setattr(m, "_budget_remaining", lambda: 10)  # plenty of budget
    monkeypatch.setattr(m, "_bot_cooldown_left", lambda: 0)  # no cooldown

    hits, cov = m._sweep_search_for_need("sensor")
    hit_ids = [h["item_id"] for h in hits]
    assert "1" in hit_ids and "2" in hit_ids and "3" in hit_ids and "4" in hit_ids
    assert len(hit_ids) == 4  # deduped across calls
    assert cov["normal_hits"] == 4
    assert cov["choice_hits"] == 1
    assert cov["kombi_hits"] == 1
    assert "Kombi" in cov["searched_channels"]


# ==============================================================================
# Requirement B: Candidate selection not by bait price + quick path partial
# ==============================================================================

def test_candidate_ids_ranks_by_relevance_and_orders_not_price(monkeypatch):
    """_candidate_ids sorts by relevance position + orders and guarantees Choice & Kombi."""
    raw_hits = [
        # Bait item: cheapest price 0.50, but low orders, pos 0
        {"item_id": "bait", "title": "Sensor wire", "price": 0.50, "orders": 2, "choice": False, "combo": False},
        # High volume item: price 15.00, 2000 orders, pos 1
        {"item_id": "popular", "title": "Sensor main", "price": 15.00, "orders": 2000, "choice": False, "combo": False},
        # Kombi item: price 8.00, pos 5, orders 50
        {"item_id": "kombi1", "title": "Sensor bundle", "price": 8.00, "orders": 50, "choice": False, "combo": True},
        # Choice item: price 9.00, pos 6, orders 100
        {"item_id": "choice1", "title": "Sensor choice", "price": 9.00, "orders": 100, "choice": True, "combo": False},
    ]

    monkeypatch.setattr(m, "_sweep_search_for_need", lambda *a, **k: (raw_hits, {"searched_channels": ["normal", "Choice", "Kombi"]}))
    spec = {"query": "sensor", "match": "sensor", "avoid": ""}

    cands = m._candidate_ids(spec)
    # Popular item beats bait item despite higher price because of log2(orders) bonus
    assert cands.index("popular") < cands.index("bait")
    # Both Kombi and Choice items are guaranteed to be in candidates
    assert "kombi1" in cands
    assert "choice1" in cands


def test_quick_path_marks_partial_ranking_when_capped(monkeypatch):
    """_rank_for_pieces marks partial ranking when fresh pages hit limit."""
    hits = [{"item_id": f"item_{i}", "title": f"Relay {i}", "orders": 10} for i in range(10)]
    monkeypatch.setattr(m, "_PDP_CACHE", {})
    monkeypatch.setattr(m, "FRESH_PAGES_QUICK", 2)
    monkeypatch.setattr(m, "_bot_cooldown_left", lambda: 0)
    fake_way = {
        "net": 5.0, "total": 5.0, "goods": 5.0, "ship": 0.0, "duty": 0.0, "save": 0.0, "pieces": 1,
        "lines": [({"sku_id": "1", "label": "Default"}, 1)],
        "offer": {"choice": False, "item_id": "item_0", "store": ""},
    }
    monkeypatch.setattr(m, "_ways_for", lambda iid, spec, fee: ([dict(fake_way, offer=dict(fake_way["offer"], item_id=iid))], []))

    cov = {"searched_channels": ["normal"], "normal_hits": 10, "choice_hits": 0, "kombi_hits": 0, "priced": 0, "skipped": []}
    out = m._rank_for_pieces("relay", hits, 1, "", "", rank_top=10, choice_only=False, line=None, cov=cov)

    # Output should indicate partial results
    assert "Teilergebnis:" in out
    assert "noch nicht geprüft" in out
    assert cov["priced"] == 2
    assert any(reason == "Aufruflimit" for reason, _ in cov["skipped"])


# ==============================================================================
# Requirement C: Coverage line on results and no silent drops
# ==============================================================================

def test_format_coverage():
    """_format_coverage produces readable summary of search channels, hits, and skips."""
    cov = {
        "searched_channels": ["normal", "Choice", "Kombi"],
        "normal_hits": 30,
        "choice_hits": 5,
        "kombi_hits": 4,
        "priced": 6,
        "skipped": [("Aufruflimit", 2), ("Botsperre", 1)],
    }
    line = m._format_coverage(cov)
    assert "Kombi: 4" in line
    assert "Choice: 5" in line
    assert "normal: 30, 6 durchgerechnet, 3 übersprungen (2 Aufruflimit, Botsperre)" in line


def test_price_hits_records_skipped_errors_and_bot_cooldown(monkeypatch):
    """_price_hits does not silently drop items when exceptions or bot cooldown occur."""
    hits = [
        {"item_id": "err_item", "title": "Motor", "orders": 5},
        {"item_id": "bot_item", "title": "Motor 2", "orders": 5},
    ]
    monkeypatch.setattr(m, "_PDP_CACHE", {})
    monkeypatch.setattr(m, "FRESH_PAGES_QUICK", 10)

    # First item raises exception, second item hits bot cooldown
    def mock_ways(iid, spec, fee):
        if iid == "err_item":
            raise RuntimeError("API timeout")
        return [], []

    monkeypatch.setattr(m, "_ways_for", mock_ways)
    bot_cooldown = [0, 60]  # second item sees bot cooldown
    monkeypatch.setattr(m, "_bot_cooldown_left", lambda: bot_cooldown.pop(0))

    cov = {"searched_channels": ["normal"], "normal_hits": 2, "choice_hits": 0, "kombi_hits": 0, "priced": 0, "skipped": []}
    rows, notes, skipped = m._price_hits(hits, 1, "", "", rank_top=5, choice_only=False, cov=cov)

    assert len(rows) == 0
    # Both skipped items are tracked in cov["skipped"] with reasons
    reasons = [reason for reason, _ in cov["skipped"]]
    assert any("Fehler" in r for r in reasons)
    assert any("Botsperre" in r for r in reasons)


# ==============================================================================
# Requirement D: Server-computed decide rows, duty, same-item ways, combo flag
# ==============================================================================

def test_compute_decide_rows_from_cached_pdp(monkeypatch):
    """_compute_decide_rows populates pack, price, ship, choice from _pdp_offer."""
    fake_offer = {
        "item_id": "111",
        "choice": True,
        "variants": [{"sku_id": "s1", "pack": 5, "price": 2.50}],
        "shipping": {"free": True, "fee": 0.0, "days": (5, 8)},
    }
    monkeypatch.setattr(m, "_pdp_offer", lambda iid: fake_offer if iid == "111" else None)

    cats = [{
        "heading": "Kondensator",
        "need": 10,
        "cards": [
            {"item_id": "111", "sku_id": "s1"},
        ]
    }]

    m._compute_decide_rows(cats)
    card = cats[0]["cards"][0]
    assert card["pack"] == 5
    assert card["price"] == 2.50
    assert card["qty"] == 2  # 10 needed // 5 per pack = 2
    assert card["pieces"] == 10
    assert card["choice"] is True
    assert card["duty"] == m.DUTY_PER_LINE      # Choice pays import charge like in _ways_for; only Kombi is free
    assert card["ship"] == 0.0


def test_compute_decide_rows_unreadable_fallback(monkeypatch):
    """_compute_decide_rows keeps user values and marks 'Preis nicht geprüft' if unreadable."""
    monkeypatch.setattr(m, "_pdp_offer", lambda iid: None)

    cats = [{
        "heading": "Sicherung",
        "need": 2,
        "cards": [
            {"item_id": "unreadable_99", "sku_id": "s1", "price": 1.20, "pack": 1},
        ]
    }]

    m._compute_decide_rows(cats)
    card = cats[0]["cards"][0]
    assert card["badge"] == "Preis nicht geprüft"
    assert "Preis nicht geprüft" in card["tags"]
    assert card["price"] == 1.20
    assert card["pack"] == 1


def test_compute_decide_rows_same_item_multi_row_duty(monkeypatch):
    """Same-item multi-row ways show duty once at group level and 0.0 on row price."""
    fake_offer = {
        "item_id": "global_item",
        "choice": False,
        "variants": [
            {"sku_id": "v1", "pack": 1, "price": 1.00},
            {"sku_id": "v2", "pack": 5, "price": 4.00},
        ],
        "shipping": {"free": False, "fee": 2.00, "days": (10, 15)},
    }
    monkeypatch.setattr(m, "_pdp_offer", lambda iid: fake_offer if iid == "global_item" else None)

    cats = [{
        "heading": "Widerstand",
        "need": 6,
        "cards": [
            {"item_id": "global_item", "sku_id": "v1", "qty": 1, "way": "way1"},
            {"item_id": "global_item", "sku_id": "v2", "qty": 1, "way": "way1"},
        ]
    }]

    m._compute_decide_rows(cats)
    c1, c2 = cats[0]["cards"]
    # Duty is NOT folded into individual rows
    assert c1["duty"] == 0.0
    assert c2["duty"] == 0.0
    # Group duty is set once
    assert c1["item_duty"] == m.DUTY_PER_LINE
    assert c2["item_duty"] == m.DUTY_PER_LINE
    assert c1["group_item"] == "global_item"
    assert c2["group_item"] == "global_item"
    # Shared shipping: first row keeps shipping, second has 0.0
    assert c1["ship"] == 2.00
    assert c2["ship"] == 0.0


def test_compute_decide_rows_single_global_row_computes_duty_from_server(monkeypatch):
    """Single global row computes duty from DUTY_PER_LINE and ignores model-typed duty/ship."""
    fake_offer = {
        "item_id": "global_single",
        "choice": False,
        "variants": [{"sku_id": "v1", "pack": 1, "price": 5.00}],
        "shipping": {"free": False, "fee": 1.80, "days": (10, 15)},
    }
    monkeypatch.setattr(m, "_pdp_offer", lambda iid: fake_offer if iid == "global_single" else None)

    cats = [{
        "heading": "Modul",
        "need": 1,
        "cards": [
            # Model hallucinated 0 duty and 0 ship
            {"item_id": "global_single", "sku_id": "v1", "qty": 1, "duty": 0.0, "ship": 0.0},
        ]
    }]

    m._compute_decide_rows(cats)
    card = cats[0]["cards"][0]
    # Server computed from PDP data: DUTY_PER_LINE used, shipping fee populated
    assert card["duty"] == m.DUTY_PER_LINE
    assert card["ship"] == 1.80
    assert "Preis nicht geprüft" not in card.get("tags", [])


def test_compute_decide_rows_different_variants_stay_alternatives(monkeypatch):
    """Different variants of the same listing stay alternatives with their own shipping and duty."""
    fake_offer = {
        "item_id": "same_item",
        "choice": False,
        "variants": [
            {"sku_id": "v1", "pack": 1, "price": 2.00},
            {"sku_id": "v2", "pack": 1, "price": 3.00},
        ],
        "shipping": {"free": False, "fee": 2.50, "days": (10, 15)},
    }
    monkeypatch.setattr(m, "_pdp_offer", lambda iid: fake_offer if iid == "same_item" else None)

    cats = [{
        "heading": "Optionen",
        "need": 1,
        "cards": [
            {"item_id": "same_item", "sku_id": "v1", "qty": 1},
            {"item_id": "same_item", "sku_id": "v2", "qty": 1},
        ]
    }]

    m._compute_decide_rows(cats)
    c1, c2 = cats[0]["cards"]
    # They are alternative options, not meant to be bought together
    assert c1["duty"] == m.DUTY_PER_LINE
    assert c2["duty"] == m.DUTY_PER_LINE
    assert c1["ship"] == 2.50
    assert c2["ship"] == 2.50
    assert "group_item" not in c1
    assert "group_item" not in c2


def test_compute_decide_rows_same_variant_packs_grouped_as_way(monkeypatch):
    """Same sku/variant with different packs or counts group together as a way."""
    fake_offer = {
        "item_id": "pack_item",
        "choice": False,
        "variants": [
            {"sku_id": "v_same", "pack": 5, "price": 2.00},
        ],
        "shipping": {"free": False, "fee": 2.20, "days": (10, 15)},
    }
    monkeypatch.setattr(m, "_pdp_offer", lambda iid: fake_offer if iid == "pack_item" else None)

    cats = [{
        "heading": "Widerstand 10x",
        "need": 10,
        "cards": [
            {"item_id": "pack_item", "sku_id": "v_same", "qty": 1, "pack": 5},
            {"item_id": "pack_item", "sku_id": "v_same", "qty": 1, "pack": 5},
        ]
    }]

    m._compute_decide_rows(cats)
    c1, c2 = cats[0]["cards"]
    assert c1["group_item"] == "pack_item"
    assert c2["group_item"] == "pack_item"
    assert c1["ship"] == 2.20
    assert c2["ship"] == 0.0
    assert c1["duty"] == 0.0
    assert c2["duty"] == 0.0
    assert c1["item_duty"] == m.DUTY_PER_LINE



def test_decide_page_renders_group_box_and_combo_flag(tmp_path, monkeypatch):
    """decide_page renders group box for same-item multi-rows and data-combo='1'."""
    monkeypatch.setattr(m, "_report_image", lambda c: "data:image/png;base64,AA==")
    out = {}
    monkeypatch.setattr(m, "_write_report", lambda name, page: out.setdefault("page", page) and name)

    cats = [{
        "heading": "Widerstand",
        "need": 6,
        "coverage": "searched: normal, Choice, Kombi; hits: 20",
        "cards": [
            {"title": "1-Pack", "item_id": "item1", "sku_id": "v1", "price": 1.00, "pack": 1, "qty": 1,
             "group_item": "item1", "item_duty": 3.57, "duty": 0.0, "combo": True},
            {"title": "5-Pack", "item_id": "item1", "sku_id": "v2", "price": 4.00, "pack": 5, "qty": 1,
             "group_item": "item1", "item_duty": 3.57, "duty": 0.0, "combo": True},
        ]
    }]

    m.decide_report("test_page", "Test", cats, list_name="MyList")
    page = out["page"]

    assert 'class="cat-cov"' in page
    assert "searched: normal, Choice, Kombi" in page
    assert 'class="grp way-grp"' in page
    assert "gehören zusammen" in page
    assert 'data-combo="1"' in page
    assert 'LIST_NAME = "MyList"' in page


# ==============================================================================
# Requirement E: "In den Korb" flow tags list_name, need, match, avoid
# ==============================================================================

def test_apply_selection_tags_list_name_and_need_match_avoid(monkeypatch):
    """apply_selection extracts list_name and row-level need/match/avoid and tags cart lines."""
    added_regular = []
    added_combo = []

    def mock_add_to_cart(item_id, sku_id, quantity=1, list_name="", need="", combo=False, force=False, **kw):
        entry = {
            "item_id": item_id,
            "sku_id": sku_id,
            "qty": quantity,
            "list_name": list_name,
            "need": need,
            "match": kw.get("match", ""),
            "avoid": kw.get("avoid", ""),
        }
        if combo:
            added_combo.append(entry)
        else:
            added_regular.append(entry)
        return "Added line 123"

    monkeypatch.setattr(m, "add_to_cart", mock_add_to_cart)

    msg = json.dumps({
        "action": "select",
        "list_name": "Elektronik-Projekt",
        "items": [
            {
                "item_id": "101",
                "sku_id": "201",
                "qty": 2,
                "state": "yes",
                "need": "Microcontroller",
                "match": "ESP32-S3",
                "avoid": "CH340",
                "combo": False,
            },
            {
                "item_id": "102",
                "sku_id": "202",
                "qty": 5,
                "state": "yes",
                "need": "Sockel",
                "match": "DIP-8",
                "avoid": "SMD",
                "combo": True,
            },
            {
                "item_id": "103",
                "sku_id": "203",
                "qty": 1,
                "state": "no",  # rejected
            }
        ]
    })

    result = m.apply_selection(msg)
    assert "Added line 123" in result

    # Regular line checks
    assert len(added_regular) == 1
    r = added_regular[0]
    assert r["item_id"] == "101"
    assert r["list_name"] == "Elektronik-Projekt"
    assert r["need"] == "Microcontroller"
    assert r["match"] == "ESP32-S3"
    assert r["avoid"] == "CH340"

    # Combo line checks
    assert len(added_combo) == 1
    c = added_combo[0]
    assert c["item_id"] == "102"
    assert c["list_name"] == "Elektronik-Projekt"
    assert c["need"] == "Sockel"
    assert c["match"] == "DIP-8"
    assert c["avoid"] == "SMD"


def test_add_to_cart_persists_match_and_avoid_in_line_store(tmp_path, monkeypatch):
    """add_to_cart saves match and avoid fields into _line_store for regular and combo lines."""
    monkeypatch.setattr(m, "MCP_LINES_FILE", tmp_path / "lines.json")
    monkeypatch.setattr(m, "load_cookies", lambda: {"session": "dummy"})
    monkeypatch.setattr(m, "_cart_add", lambda *a, **k: "c99")
    monkeypatch.setattr(m, "_cart_pages", lambda *a, **k: ({}, {}))
    fake_line = {
        "cart_id": "c99", "item_id": "555", "sku_id": "666", "qty": 1,
        "title": "Tip", "variant": "T12", "price": 2.0, "ship": 0.0,
        "group": "global", "checked": True, "delivery": ("2026-12-01", "2026-12-10"),
    }
    monkeypatch.setattr(cartlib, "parse", lambda *a, **k: {"lines": [fake_line]})
    monkeypatch.setattr(m, "_read_cart", lambda *a, **k: {"lines": [fake_line]})

    res = m.add_to_cart(
        item_id="555",
        sku_id="666",
        quantity=1,
        list_name="Werkstatt",
        need="Lötspitzen",
        match="T12",
        avoid="900M",
        force=True,
    )
    assert "cart_id c99" in res

    store = m._line_store()
    saved_tag = store.get("c99")
    assert saved_tag is not None
    assert saved_tag["list"] == "Werkstatt"
    assert saved_tag["need"] == "Lötspitzen"
    assert saved_tag["match"] == "T12"
    assert saved_tag["avoid"] == "900M"

    # Also test combo cart line
    monkeypatch.setattr(m, "_cart_add", lambda *a, **k: "c100")
    fake_combo_line = {
        "cart_id": "c100", "item_id": "777", "sku_id": "888", "qty": 3,
        "title": "Combo Tip", "variant": "T12", "price": 1.5, "ship": 0.0,
        "group": "combo", "checked": True, "delivery": ("2026-12-01", "2026-12-10"),
    }
    monkeypatch.setattr(m, "_read_cart", lambda *a, **k: {"lines": [fake_combo_line]})
    res_combo = m.add_to_cart(
        item_id="777",
        sku_id="888",
        quantity=3,
        list_name="Werkstatt",
        need="Kombi-Spitzen",
        match="T12-BC2",
        avoid="T12-K",
        combo=True,
        force=True,
    )
    assert "cart_id c100" in res_combo

    store2 = m._line_store()
    saved_combo_tag = store2.get("c100")
    assert saved_combo_tag is not None
    assert saved_combo_tag["list"] == "Werkstatt"
    assert saved_combo_tag["need"] == "Kombi-Spitzen"
    assert saved_combo_tag["match"] == "T12-BC2"
    assert saved_combo_tag["avoid"] == "T12-K"
    assert saved_combo_tag["combo"] is True


def test_candidate_ids_hard_cap_25_prefer_kombi_then_choice(monkeypatch):
    """_candidate_ids enforces a hard cap of 25 candidates, prioritizing Kombi then Choice."""
    # 30 kombi hits, 30 choice hits, 10 regular hits
    raw_hits = []
    for i in range(30):
        raw_hits.append({"item_id": f"k_{i}", "title": f"Sensor Kombi {i}", "price": 5.0, "orders": 100, "choice": False, "combo": True})
    for i in range(30):
        raw_hits.append({"item_id": f"c_{i}", "title": f"Sensor Choice {i}", "price": 4.0, "orders": 200, "choice": True, "combo": False})
    for i in range(10):
        raw_hits.append({"item_id": f"r_{i}", "title": f"Sensor Reg {i}", "price": 3.0, "orders": 500, "choice": False, "combo": False})

    monkeypatch.setattr(m, "_sweep_search_for_need", lambda *a, **k: (raw_hits, {"searched_channels": ["normal", "Choice", "Kombi"]}))
    spec = {"query": "sensor", "match": "sensor", "avoid": ""}

    cands = m._candidate_ids(spec)
    assert len(cands) == 25
    # All 25 should be Kombi items because we have 30 Kombi items and Kombi has priority 1
    assert all(cid.startswith("k_") for cid in cands)

    # Now test with 10 Kombi, 20 Choice, 10 regular: 10 Kombi + 15 Choice = 25
    raw_hits2 = raw_hits[:10] + raw_hits[30:50] + raw_hits[60:]
    monkeypatch.setattr(m, "_sweep_search_for_need", lambda *a, **k: (raw_hits2, {"searched_channels": ["normal", "Choice", "Kombi"]}))
    cands2 = m._candidate_ids(spec)
    assert len(cands2) == 25
    kombi_cands = [c for c in cands2 if c.startswith("k_")]
    choice_cands = [c for c in cands2 if c.startswith("c_")]
    assert len(kombi_cands) == 10
    assert len(choice_cands) == 15


def test_format_coverage_honest_kombi_not_searched():
    """_format_coverage accurately reports when Kombi sweep was not performed due to budget/cooldown."""
    cov_budget = {
        "searched_channels": ["normal", "Choice"],
        "normal_hits": 10,
        "choice_hits": 2,
        "kombi_hits": 0,
        "kombi_searched": False,
        "kombi_reason": "Budget",
        "priced": 2,
        "skipped": [],
    }
    line = m._format_coverage(cov_budget)
    assert "Kombi: nicht gesucht (Budget)" in line

    cov_bot = {
        "searched_channels": ["normal", "Choice"],
        "normal_hits": 10,
        "choice_hits": 2,
        "kombi_hits": 0,
        "kombi_searched": False,
        "kombi_reason": "Botsperre",
        "priced": 2,
        "skipped": [],
    }
    line2 = m._format_coverage(cov_bot)
    assert "Kombi: nicht gesucht (Botsperre)" in line2

    cov_default = {
        "searched_channels": ["normal", "Choice"],
        "normal_hits": 10,
        "choice_hits": 2,
        "kombi_hits": 0,
        "kombi_searched": False,
        "priced": 2,
        "skipped": [],
    }
    line_def = m._format_coverage(cov_default)
    assert "Kombi: nicht gesucht (Budget/Botsperre)" in line_def

    cov_searched = {
        "searched_channels": ["normal", "Choice", "Kombi"],
        "normal_hits": 10,
        "choice_hits": 2,
        "kombi_hits": 0,
        "kombi_searched": True,
        "priced": 2,
        "skipped": [],
    }
    line3 = m._format_coverage(cov_searched)
    assert "Kombi: gesucht" in line3


def test_sweep_search_at_most_two_extra_pages_and_cooldown(monkeypatch):
    """_sweep_search_for_need adds at most 2 extra pages and stops during bot cooldown."""
    calls = []
    def mock_search(query, sort_by="default", page=1, choice_only=False):
        calls.append((sort_by, page))
        return [{"item_id": f"id_{sort_by}_{page}", "title": "Hit", "combo": page == 2}]

    monkeypatch.setattr(m, "_search", mock_search)
    monkeypatch.setattr(m, "_budget_remaining", lambda: 10)
    monkeypatch.setattr(m, "_bot_cooldown_left", lambda: 0)

    hits, cov = m._sweep_search_for_need("relay")
    # Base search (best_match, p1) + at most 2 extra pages: (orders, p1) and (best_match, p2)
    assert len(calls) == 3
    assert calls == [("best_match", 1), ("orders", 1), ("best_match", 2)]
    assert cov["kombi_searched"] is True

    # Test cooldown stops extra pages
    calls.clear()
    monkeypatch.setattr(m, "_bot_cooldown_left", lambda: 30)
    hits2, cov2 = m._sweep_search_for_need("relay")
    assert len(calls) == 1  # only base search ran
    assert cov2["kombi_searched"] is False
    assert cov2["kombi_reason"] == "Botsperre"


def test_recheck_cart_sweeps_kombi_and_reports_coverage(tmp_path, monkeypatch):
    """_recheck_run uses _sweep_search_for_need and includes coverage in output."""
    lines_file = tmp_path / "lines.json"
    lines_file.write_text(json.dumps({"c1": {"list": "TestList", "need": "ESP32", "match": "ESP32", "avoid": ""}}))
    monkeypatch.setattr(m, "MCP_LINES_FILE", lines_file)
    monkeypatch.setattr(m, "load_cookies", lambda: {"session": "dummy"})
    fake_line = {
        "cart_id": "c1", "item_id": "item1", "sku_id": "s1", "qty": 1,
        "title": "ESP32", "variant": "Default", "price": 5.0, "ship": 0.0,
        "group": "choice", "checked": True, "delivery": ("2026-12-01", "2026-12-10"),
    }
    monkeypatch.setattr(m, "_read_cart", lambda *a, **k: {"lines": [fake_line]})

    cov_mock = {
        "searched_channels": ["normal", "Choice", "Kombi"],
        "normal_hits": 5, "choice_hits": 2, "kombi_hits": 1,
        "kombi_searched": True, "priced": 1, "skipped": []
    }
    monkeypatch.setattr(m, "_sweep_search_for_need", lambda q: ([{"item_id": "item1", "title": "ESP32", "orders": 10}], cov_mock))
    monkeypatch.setattr(m, "_price_hits", lambda *a, **k: ([], [], []))
    monkeypatch.setattr(m, "_current_way", lambda *a, **k: {"net": 5.0})

    out = m._recheck_run("TestList", rank_top=3, max_lines=1, skip=0, progress=lambda *a: None)
    assert "Kombi: 1" in out
    assert "Choice: 2" in out


def test_report_eta_js_null_guard():
    """report.py contains the null guard in eta() to prevent crash on .grp elements without .gx."""
    assert 'var x = g.querySelector(".gx");' in report._DECIDE_JS
    assert 'if (!x) return;' in report._DECIDE_JS

