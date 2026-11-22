# Walkthrough: Kombi Sweep, Coverage Line, Server-Computed Decide Rows & Selection Tagging

This walkthrough documents the design, implementation, and verification of universal Kombi sweeping, candidate selection, coverage tracking, server-computed decide rows, and list/tagging propagation into `_line_store()`.

---

## 1. Key Findings & Architecture Decisions

### PDP Kombi Marker Investigation
- **Finding:** Inspected `docs/mtop-notes.md` and 25 real cached PDP MTOP responses (`mtop.aliexpress.pdp.pc.detail` and `mtop.aliexpress.pdp.pc.query`). AliExpress detail responses **do not** carry a Kombi/bundle deal marker on the product page detail itself.
- **Where Kombi Markers Exist:**
  1. **Search & Channel level:** `rainbow.rainbowType == "channelProduct"` (title: "Kombi-Knaller"), `sellingPoints` containing `source: "npieces"`, card badges with bundle text/classes.
  2. **Cart level:** `businessCartInfo.businessCarts` with `nn_mix` channel identifier.
- **Decision:** As instructed, we do not invent a synthetic PDP marker. Instead, Kombi item IDs identified during search channel sweeps are persisted to disk in `~/.mcp-credentials/aliexpress-combo-cache.json` with TTL (`PDP_DISK_TTL`), loaded on startup, and kept in memory in `COMBO_ITEMS`. When `decide_report` or `_compute_decide_rows` inspects an offer, it checks `_is_combo(item_id)` and preserves the `combo` flag.

### Candidate Ranking & Anti-Bait Logic
- **Problem:** Sorting candidates by raw search listing price grabbed cheap accessories (0.40 € mounting brackets, bare cables) rather than the intended main product.
- **Solution:** `_candidate_ids` scores matching hits by relevance position discounted by sales volume: `pos - 3.0 * math.log2(orders + 1)`.
- **Guaranteed Inclusion:** Every Kombi hit and every Choice hit that satisfies the search `match` / `avoid` criteria is guaranteed to be included in candidate evaluation regardless of initial rank, capped at max 25 candidates.
- **Partial Ranking Transparency:** Quick path `search_products(pieces=N)` prices up to `FRESH_PAGES_QUICK` (4) new product pages. When hits remain unpriced, output is explicitly flagged as `"Teilergebnis: X von Y Treffern durchgerechnet, Z noch nicht geprüft"` rather than presenting a partial ranking as complete.

### Same-Item Multi-Row Ways (Pack Splitting & Duty)
- **Problem:** When covering a need with multiple packs of the same product (e.g. 2× a 3-pack + 1× a 1-pack), attaching duty (3.57 €) to every row double-counts the import fee and inflates row unit prices.
- **Solution:**
  - `_compute_decide_rows` groups same-item rows within a category.
  - Rows get `duty = 0.0` so duty is **never folded into the row price**.
  - Group duty is attached once at the group/order level: `item_duty = DUTY_PER_LINE` (3.57 € for global sellers, 0.0 € for Choice or Kombi).
  - Subsequent rows for global sellers have shared shipping (`ship = 0.0`).
  - `report.py` wraps them in `<div class="grp way-grp">` with header `"Ein Weg (N Stück, M Zeilen): Ware X € + 3,57 € Abgabe (gehören zusammen)"`.
  - JS `calc()` deduplicates `itemDuty` per `itemKey` so the order total calculates import duty exactly once.

---

## 2. Changes by Component

### A. Kombi Sweeping & Disk Persistence
- **`aliexpress_mcp_server.py`:**
  - Implemented `_combo_disk_load()`, `_combo_disk_store(item_id)`, `_is_combo(item_id)`, and `_remember_combo(item_id)`.
  - Initialized `COMBO_ITEMS = set(_combo_disk_load().keys())`.
  - Configured `_COMBO_FILE` (`~/.mcp-credentials/aliexpress-combo-cache.json`) with `COMBO_DISK_TTL`.
  - Updated `_parse_search_cards` and `_parse_search_json` to detect `combo: True` and persist seen items.
  - Implemented `_sweep_search_for_need(query, ...)` to sweep pages 1 & 2 across default and orders sorts, deduplicating hits by `item_id`.

### B. Candidate Ranking & Partial Search Results
- **`aliexpress_mcp_server.py`:**
  - Updated `_candidate_ids(spec)`: ranks hits by `pos - 3.0 * math.log2(orders + 1)`, guarantees inclusion of all Kombi and Choice hits that pass match criteria, capped at max 25.
  - Updated `_rank_for_pieces`: explicitly flags capped rankings with `"Teilergebnis: ..."` when unpriced hits remain.

### C. Coverage Reporting & Zero Silent Drops
- **`aliexpress_mcp_server.py`:**
  - Implemented `_format_coverage(cov)`: outputs single-line summary of channels searched (normal, Choice, Kombi), hit counts per channel, priced counts, and skipped items with breakdown by reason (`Aufruflimit`, `Botsperre`, `Fehler`).
  - Updated `search_products`, `_rank_for_pieces`, `_plan`, `_fmt_plan`, `_chat_plan`, `_line_deal`, and `recheck_cart` to attach and display coverage lines.
  - Recorded skipped items from `_price_hits` and `_line_deal` (call limit, bot cooldown, exceptions) without silent omissions.
- **`report.py`:**
  - Added `.cat-cov` styling and rendered `sec["coverage"]` in category headers.

### D. Server-Computed Decide Rows
- **`aliexpress_mcp_server.py`:**
  - Implemented `_compute_decide_rows(categories)`: computes `pack`, `price`, `ship`, `choice`, `combo`, and `duty` from cached product page (`_pdp_offer`).
  - Fallback logic: when product page is unreadable, preserves user-specified values and attaches `"Preis nicht geprüft"` badge and tag.
  - Handles same-item multi-row ways: `duty = 0.0` per row, `item_duty` at group level, shared shipping.
  - Sets `data-combo="1"` on combo cards so `apply_selection` routes them to combo cart.
  - Updated `decide_report(name, title, categories, list_name="...")` to run `_compute_decide_rows` and forward `list_name`.
- **`report.py`:**
  - Added `.grp.way-grp` styling for same-item multi-row ways ("gehören zusammen").
  - Updated `_decide_row` to output `data-need`, `data-match`, `data-avoid`, `data-item-duty`, `data-combo`.
  - Updated `_DECIDE_JS`: includes `list_name`, `need`, `match`, `avoid` in button selection payload; deduplicates duty via `seenDuty[itemKey]`.

### E. "In den Korb" Selection Tagging
- **`aliexpress_mcp_server.py`:**
  - Updated `add_to_cart`: accepts `match` and `avoid` kwargs, saves `list`, `need`, `match`, and `avoid` into `_line_store()` (`~/.mcp-credentials/aliexpress-lines.json`) for both regular cart lines and combo cart lines.
  - Updated `apply_selection`: extracts `list_name` from message payload, extracts row `need`, `match`, `avoid`, and passes them to `add_to_cart`.

### F. Documentation & Rules
- **`SKILL.md`:**
  - Updated pack-size rule: documented automatic resolution from listing LOT / numberPerLot, unitContentSkuMap, variant names ("5PCS", "10 Stück", "5 Paar"), and listing title.
  - Updated Kombi rule: documented universal Kombi channel sweeping when searching for needs and `combo_only` filtering.
- **Docstrings:** Updated in `aliexpress_mcp_server.py` for `combo_only`, `best_offer`, `get_product_variants`, `decide_report`, and `search_products`.

---

## 3. Test Suite & Verification

- **New Test File (`tests/test_kombi_sweep_coverage.py`):**
  - `test_combo_disk_persistence`: Verifies disk persistence, TTL expiry, and `_is_combo` queries.
  - `test_parse_search_cards_flags_and_remembers_combo`: Verifies search parser sets `combo: True` and stores to cache.
  - `test_sweep_search_for_need_merges_channels_and_dedupes`: Verifies multi-channel sweeping, deduplication, and coverage stats.
  - `test_candidate_ids_ranks_by_relevance_and_orders_not_price`: Verifies logarithmic orders bonus beats bait prices and includes Choice & Kombi.
  - `test_quick_path_marks_partial_ranking_when_capped`: Verifies partial ranking indicator when page limit reached.
  - `test_format_coverage`: Verifies single-line coverage format with channel and skip summaries.
  - `test_price_hits_records_skipped_errors_and_bot_cooldown`: Verifies error and cooldown tracking in coverage without silent drops.
  - `test_compute_decide_rows_from_cached_pdp`: Verifies PDP offer facts population.
  - `test_compute_decide_rows_unreadable_fallback`: Verifies fallback values and "Preis nicht geprüft" badge.
  - `test_compute_decide_rows_same_item_multi_row_duty`: Verifies `duty = 0.0` on rows, single `item_duty` on group, and shared shipping.
  - `test_decide_page_renders_group_box_and_combo_flag`: Verifies way group rendering, coverage display, and combo flags.
  - `test_apply_selection_tags_list_name_and_need_match_avoid`: Verifies selection parsing and keyword tagging.
  - `test_add_to_cart_persists_match_and_avoid_in_line_store`: Verifies regular and combo lines save list, need, match, and avoid in `_line_store()`.
- **Test Safety & Zero Network Leakage (`tests/conftest.py`):**
  - Added fixture to intercept and block any outgoing live HTTP calls during tests.
  - Added cleanup fixture for in-memory and disk bot check state.
- **Full Test Results:**
  - 193 passed in 27.72s with zero failures.
