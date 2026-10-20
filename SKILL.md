Use this skill when the user mentions AliExpress, searching for products on
AliExpress, checking prices from China, finding cheap alternatives, or
references an AliExpress item URL or item ID. It searches, compares offers,
reads reviews, collects store coupons and manages the cart (add, change
quantity, remove, check for checkout). It does NOT check out or pay: it hands
the user the checkout link.

Before searching, ask the user once — unless they already said — whether
global sellers are fine (often cheaper, 2–4 weeks, shipping per cart line) or
only Choice / "Shipped by AliExpress" (about a week, free above ~10 €). Pass
the answer as `choice_only=True` (search_products, shop_list) or per item
`seller: "choice"`. With a mixed answer, show both prices: run shop_list with
and without choice_only and say what the speed costs.

The usual job — a shopping list into a ready cart — is one tool:

1. `shop_list(items)` with one dict per part: `need`, `pieces`, and `query`
   (search) and/or `item_ids` (known candidates), optionally `match` / `avoid`
   words for the variant, or `sku_id` + `qty` to take an exact variant. It
   prices every candidate as checkout will (shipping per cart line for global
   sellers, Choice free-shipping threshold, store discounts, delivery days)
   and prints the plan with the runner-up per part.
2. Read the notes. "variant names repeat" or "!" (suspiciously cheap
   variant): call `get_variant_images` and look; then set `match`. For parts
   that are often faked (ICS-43434, branded chips) read
   `get_product_reviews(item_id, only_low=True)`.
3. `shop_list(items, apply=True)` once the plan is right. It adds or resizes
   the lines, removes the MCP's own earlier lines for these products that the
   plan replaced (never the user's own lines), collects the store coupon worth
   having, unchecks everything else, checks the plan, and returns the
   checkout link with every plan line. It ends with "Plan vs cart": any price,
   shipping or delivery difference is listed — tell the user.
4. Give the user the checkout link and the totals. Paying is their click.

Single questions: `best_offer(item_id, pieces)` (cheapest way to N pieces from
one product), `get_discounts(item_id, subtotal)`, `collect_coupon(item_id,
subtotal)`, `view_cart()` (grouped cart with a shipping analysis),
`select_cart_items(cart_ids, only=True)`.

Rules worth remembering:
- Choice ("Shipped by AliExpress") ships free once the checked Choice lines
  together pass about 10 €, in 4–10 days. Global sellers charge shipping per
  cart line and often take 2–4 weeks. Two variants of one product are two
  lines and pay shipping twice; `shop_list` prefers one line.
- AliExpress remembers at most 10 checked cart lines (an 11th unchecks the
  oldest, on the web page too). The checkout link has no such limit — always
  hand it out.
- The search price is the cheapest variant and often bait (an accessory, a
  bare chip). Never compare offers by it.
- Generic variant names hide different articles: "Chip" was the bare
  ESP32-S3 module, "Module" the dev board. Put the kind of article into
  `match` ("N16R8 board", not just "N16R8"): the cheapest "ESP32-S3 N16R8"
  hits were bare WROOM modules without USB, and expansion boards without
  the chip. Look at a winner's picture when its price is far below the rest.
- A search returns 60 hits; shop_list tries the `candidates` cheapest whose
  title has the match words (default 5) — raise it for wide markets.
- Coupons: only automatic and collected coupons count in the ranking.
  `collect_coupon` / apply collect plain "Collect" coupons in a browser
  (AliExpress accepts that only from its own page); "Follow & collect" is
  never taken, since it would follow the store. One coupon per store and order.
- Bot check: calls are paced (ALIEXPRESS_MTOP_GAP, 0.8 s) and product pages
  cached 10 min. A reCAPTCHA box is clicked automatically; if lookups still
  say "bot check not passed", stop calling for 15-30 min (every call extends it).
- Prices and shipping are for ALIEXPRESS_COUNTRY (default DE, EUR). Coins are
  not included. A store welcome coupon may only work for a first order in
  that store. The product page price can differ from the cart price; apply
  reports it.
