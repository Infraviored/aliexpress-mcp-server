Use this skill when the user mentions AliExpress, searching for products on
AliExpress, checking prices from China, finding cheap alternatives, or
references an AliExpress item URL or item ID. It searches, compares offers,
reads reviews, and manages the cart (add, change quantity, remove). It does
NOT check out or pay.

How to find the best deal (the tools do the arithmetic; keep calls few):

1. `search_products` (sort "orders") → candidates with price, orders, rating,
   Choice, shipping line. `with_shipping=True` adds real shipping for
   non-Choice hits.
2. `best_offer(item_id, pieces)` per candidate → the cheapest way to N pieces
   (pack sizes and mixes, shipping included) plus a "tip" line when a few cents
   more buy many more pieces. Prefer it over reading raw variant lists.
3. Variant names ("Color"/"Farbe") are often pack sizes, sometimes different
   articles, and can be identical while the article differs. When names are
   vague or repeat, call `get_variant_images` — one picture with every variant
   numbered (#n matches get_product_variants).
4. `get_product_reviews(item_id, only_low=True)` before buying parts that are
   often faked (ICS-43434, branded chips).
5. `plan_basket([...])` on the final list: Choice items ship free above the
   threshold (10 € in DE); non-Choice shipping is A + B per extra unit.
6. `add_to_cart(item_id, sku_id, qty)` only when the user asked for it.

Rules worth remembering:
- The search price is the cheapest variant and often bait (an accessory, a
  bare chip). Never compare offers by it; compare best_offer totals.
- Generic variant names hide different articles: "Chip" was the bare
  ESP32-S3 module, "Module" the dev board. best_offer prints "!" when the
  winner is far cheaper than every other variant: look at the pictures.
- Bot check: calls are paced (ALIEXPRESS_MTOP_GAP, 0.8 s) and product pages
  cached 10 min. A reCAPTCHA box is clicked automatically; if lookups still
  say "bot check not passed", stop calling for 15-30 min (every call extends it).
- Always mention when a bigger pack is much cheaper per piece or nearly free
  (1 pc 0.32 €, 5 pcs 1.39 €). The tools print a "pack ladder" and "tip" line.
- A bigger pack can also be dearer per piece (10 pcs 1.54/pc vs 5 pcs 1.44/pc);
  the ladder line flags it with "!".
- Prices and shipping are for ALIEXPRESS_COUNTRY (default DE, EUR). Coins and
  coupons are not included.
