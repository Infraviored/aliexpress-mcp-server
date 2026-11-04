---
name: aliexpress
description: Shop on AliExpress — search, compare offers as checkout prices them, turn a shopping list into a ready cart and hand out the checkout link. Use for AliExpress items, URLs, item IDs, prices from China. Never pays.
---

Never check out or pay: the user gets the checkout link and clicks pay.

Before the first search, ask once (unless they said): global sellers OK
(cheaper, 2–4 weeks) or Choice only (about a week)? Pass `choice_only=True` or
per item `seller: "choice"`. Mixed answer: run shop_list both ways and say what
the speed costs.

## Shopping list → cart

1. `shop_list(items, list_name=…)` plans it. Say its "For the user" part; publish
   the page it names with the Artifact tool (same path = same link) and open it.
2. Fix what the notes flag. Repeated or suspicious variant names: look at
   `get_variant_images`, then set `match` to the kind of article ("N16R8 board",
   not "N16R8" — the cheap hits were bare modules) or pin `sku_id` + `qty`.
   Often-faked parts (ICS-43434, branded chips): `get_product_reviews(only_low=True)`.
3. `shop_list(items, list_name=…, apply=True)`. Report every "Plan vs cart" difference.
4. Give the checkout link and the checkout totals it printed.

Cart questions: `view_cart(summary=…, options=…)` — pass your recommendation;
publish its page the same way. Existing lines onto a list: `set_cart_list`.
Real total for any lines: `checkout_preview`. One product, N pieces: `best_offer`.

## Reports

Every page for the user (research, comparison, "what did you find") comes from
`make_report`, never from hand-written HTML: one layout, always with pictures.
Each card needs `item_id` (picture from the product page) or `image` (URL,
local file). Put the verdict into `notes`, the totals into `facts`, mark what
is in the cart with a "✓ im Korb" tag. The cart and the plan pages
(`view_cart`, `shop_list`) have their own builders. Publish the file it names
with the Artifact tool, or hand it over as a file if that tool is missing.

## Rules

- Choice cheaper than a global seller (goods + shipping + import charge)? Take
  Choice, don't ask.
- Buy to need. A bigger pack only when it is cheaper per piece.
- One cart line per part: every line pays an import charge, and at a global
  seller its own shipping.
- Never compare offers by the search price (cheapest variant, often bait).
- Hand out the list's checkout link, never "the ticked lines": the cart page
  keeps only 10 ticked, the link has no limit.
- Ask before `remove_from_cart` on lines the MCP did not add.
- Offer only what the user asked for. Every word of the request that decides the
  product ("High Speed", "matt", "black", brand, size) must be in the offer's
  FULL title or variant; read the title, do not shorten it into something else.
  Never show an option that is dearer and no better than another one in the same
  list (same promise, higher price): leave it out. Show 2–4 alternatives that differ in
  something the user can care about (price per piece, delivery, brand, pack), and say what.
- Pack sizes: `best_offer` reads "5PCS"/"Stück" from the variant name only. A name like
  "5 Black" (5-spool bundle) counts as ONE piece unless you pass `packs="5 black=5; 1 black=1"`;
  without it a request for 5 spools is priced as 5 bundles (25 spools). Check the product
  page when `best_offer` warns that no pack size was found.
- Fewer calls, fewer bot checks. The server pauses 2–3.5 s between calls, remembers product
  pages for 3 h on disk and, after a bot check, pauses every call for 10 min (calls fail
  fast with "bot check active"). Search first, then look closely at 2–4 candidates only,
  not every hit; never run many `best_offer` calls in a row for the same question.
- "Bot check not passed": do not keep calling (every call extends it). Fastest way out:
  `.venv/bin/python import_profile.py --ask` — the user passes the check once in a real Chrome
  (remote-browser card in the dashboard, run it in the background and tell him), the cookies
  are imported. Otherwise wait 15–30 min.
