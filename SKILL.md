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

1. `shop_list(items, list_name="…")` with one dict per part: `need`, `pieces`, and `query`
   (search) and/or `item_ids` (known candidates), optionally `match` / `avoid`
   words for the variant, or `sku_id` + `qty` to take an exact variant. It
   prices every candidate as checkout will (shipping per cart line for global
   sellers, Choice free-shipping threshold, import charges per cart line,
   store discounts, delivery days) and prints the plan with the runner-up per
   part.
   The result starts with "For the user": a few lines to say in the chat
   (no links, no pictures — the chat cannot embed them). Then the path of an
   HTML page with every offer's picture, link, price and the runners-up:
   publish it with the Artifact tool (always the same path, so the link
   stays) and open it. view_cart does the same for the cart (cart.html).
   "Details (for you)" is for your own checks.
2. Read the notes. apply refuses a variant whose name repeats in its product
   (e.g. "8 bit" was both a ring and a strip): check get_variant_images and
   pass that item's `sku_id` + `qty`. "variant names repeat" or "!" (suspiciously cheap
   variant): call `get_variant_images` and look; then set `match`. For parts
   that are often faked (ICS-43434, branded chips) read
   `get_product_reviews(item_id, only_low=True)`.
3. `shop_list(items, apply=True)` once the plan is right. It adds or resizes
   the lines, removes the MCP's own earlier lines for these products that the
   plan replaced (never the user's own lines), collects the store coupon worth
   having, unchecks everything else, checks the plan, and returns the
   checkout link with every plan line. It ends with "Plan vs cart": any price,
   shipping or delivery difference is listed — tell the user.
   Note: deletions are scoped to this list's candidate products. Lines the MCP
   added for other, unrelated lists are left alone; remove them with
   `remove_from_cart` if they are no longer needed.
   Every plan line is tagged with `list_name` and its need.
4. Give the user the checkout link and the totals from the checkout preview
   apply prints (the real checkout page: goods, shipping, import charges,
   total). Paying is their click.

"What is in my cart / what is ticked / what does it cost": `view_cart()`.
It shows each shopping list apart from the rest of the cart, every line with
its need and why it is or is not ticked, flags leftovers (same part, not on
the list), and prices each list at checkout. Lines already in the cart that
belong to a list: `set_cart_list(cart_ids, list_name, need)`.

Single questions: `best_offer(item_id, pieces)` (cheapest way to N pieces from
one product), `checkout_preview(cart_ids | list_name)` (real checkout total for
any lines), `get_discounts(item_id, subtotal)`, `collect_coupon(item_id,
subtotal)`, `select_cart_items(cart_ids, only=True)`.

Rules worth remembering:
- Choice ("Shipped by AliExpress") ships free once the checked Choice lines
  together pass about 10 €, in 4–10 days. Global sellers charge shipping per
  cart line and often take 2–4 weeks. Two variants of one product are two
  lines and pay shipping twice; `shop_list` prefers one line.
- Import charges (EU, ship to DE, measured 2026-10-21): 3.57 € per cart line
  (3 € duty + VAT), whatever its price or quantity. AliExpress pays them
  beyond the 9th Choice line; every global seller line pays. A 1.39 € part
  from a global seller costs 1.39 + ~3.30 shipping + 3.57 import. So: one
  bigger pack instead of two lines, Choice instead of global once the list
  has 9 Choice lines, and say so when "to compare" items cost duty.
- AliExpress remembers at most 10 ticked cart lines, on its server (an 11th
  unticks the oldest, also via the group or "All" boxes). It only matters for
  the cart page: the checkout link and checkout_preview take any number of
  lines — always hand out the list's link, never "the ticked lines".
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
