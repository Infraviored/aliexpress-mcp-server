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

1. `shop_list(items, list_name=…)` plans it. Say its "For the user" part; give
   the user the page address it names (served by the MCP, never published) and open it.
2. Fix what the notes flag. Repeated or suspicious variant names: look at
   `get_variant_images`, then set `match` to the kind of article ("N16R8 board",
   not "N16R8" — the cheap hits were bare modules) or pin `sku_id` + `qty`.
   Often-faked parts (ICS-43434, branded chips): `get_product_reviews(only_low=True)`.
3. `shop_list(items, list_name=…, apply=True)`. Report every "Plan vs cart" difference.
4. Give the checkout link and the checkout totals it printed.

Cart questions: `view_cart(summary=…, options=…)` — pass your recommendation;
publish its page the same way. Existing lines onto a list: `set_cart_list`.
The cart page is one page for all lists (one header, one total, rows grouped by Choice and
global, a note when the rows do not add up to the checkout). Its numbered "Zu entscheiden"
items change price or date and have no buttons: add one `decide add` card per item
(`--option` per choice, `--recommend`), as `view_cart` tells you. Items that change nothing
are notes; do not turn those into questions. (Product comparisons stay on the pick page below.)
Real total for any lines: `checkout_preview`. One product, N pieces: `best_offer`.

## Reports

**The answer to every product search is the report, nothing else.** When the user asks
you to look for something, find a better price or compare, you finish with one
`make_report` page: the recommendation on top (`chosen`, with a short reason), the
alternatives below it (1–2 sentences on what differs), `"pick": True` on the section so
he can click his choice. Give the page address (served locally by the MCP, never published). Do not ask the question in
chat, do not put it on a `decide` card (that rule is for other work, not for shopping
choices), do not write a table in chat. In chat give two or three sentences and the
recommendation; the page does the rest. Adding to the cart happens after he picked.

Every page for the user (research, comparison, "what did you find") comes from
`make_report`, never from hand-written HTML: one layout, always with pictures.
Each card needs `item_id` (picture from the product page) or `image` (URL,
local file). Put the verdict into `notes`, the totals into `facts`, mark what
is in the cart with a "✓ im Korb" tag. The cart and the plan pages
(`view_cart`, `shop_list`) have their own builders. Give the user the address
the tool names. Never publish these pages (Artifact tool or any web service): they hold the user's cart.

## Orders

`list_orders(status="", since="", details="open")` reads "My orders" (read only). It writes
`orders.json` and `orders.csv` into the report folder (path in the answer) and a report page with pictures
(send that page; the chat shows no pictures). Status is normalised (wartet_auf_zahlung, in_bearbeitung,
versandt, zugestellt, abgeschlossen, storniert, rueckerstattung) next to AliExpress' own text. `details`
adds delivery date and tracking for open orders. Cancelled orders are hidden unless `status="storniert"` or `include_cancelled=True`. It never pays, confirms or changes anything.

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
- Need several pieces (8 sensors, 5 buttons)? `search_products(..., pieces=N, match="…")` prices the top hits
  for exactly N pieces (pack ladders, shipping, import, discounts) and ranks by net total. The plain
  search price is the cheapest single variant: a dearer-looking item with a "10PCS" variant is often cheaper.
- Any saving counts, even 0.50 €. `search_products(cart_id=…)` ends with a verdict against that cart line;
  `recheck_cart(list_name=…)` goes through the lines of a list and names every swap that saves money.
- Kombi-Knaller (Bundle Deals): search hits marked "Kombi" ship in a separate cart (`add_to_cart(combo=True)`,
  `view_cart` reads it). 3 or more such pieces in the combo order pay NO import charge, which is why they are
  often much cheaper: prefer them. `shop_list` does this itself: it prices Kombi hits without the charge when the
  plan reaches 3 combo pieces (and puts the charge back when it does not), reports a "Kombi-Knaller order" apart
  from the main cart, and `apply=True` puts those lines into the combo cart. The combo cart is ordered on its own
  checkout page; "Limit exceeded, remove N items" means order it in several rounds, each with 3+ pieces
  (ALIEXPRESS_COMBO_MAX_LINES sets the limit the plan splits by, 0 = unknown). Adding a variant already in the combo
  cart REPLACES its line (new cart_id): pass the total quantity. Check the combo cart price against the plan.
- Kombi lines are handled like main cart lines: `select_cart_items`, `remove_from_cart` and `update_cart_quantity`
  take their cart_id too (an id the main cart lacks is tried in the Kombi-Knaller cart). `kombi_check(list_name=…)`
  (background job like `recheck_cart`) names the lines of a list that are cheaper as Kombi-Knaller, without import
  charge, and spreads them over orders. Searches go through the same pacing gap as every call and are cached for
  30 min; during a bot check they stop with a message instead of deepening it.
- Call budget: at most 12 AliExpress calls per minute over everything (25 per minute ended in a bot
  check, seen in the call log). A quick `search_products` with `pieces` prices at most 4 NEW product pages
  per request and names the rest as "not priced": ask again, the first ones are cached and cost nothing.
  Long jobs (`recheck_cart`, `kombi_check`) run slower instead of skipping. Do not run two of them at once.
- Fewer calls, fewer bot checks. The server pauses 2–3.5 s between calls, remembers product
  pages for 3 h on disk and, after a bot check, pauses every call for 10 min (calls fail
  fast with "bot check active"). Search first, then look closely at 2–4 candidates only,
  not every hit; never run many `best_offer` calls in a row for the same question.
- "Bot check not passed": do not keep calling (every call extends it). Fastest way out:
  `.venv/bin/python import_profile.py --ask` — the user passes the check once in a real Chrome
  (remote-browser card in the dashboard, run it in the background and tell him), the cookies
  are imported. Otherwise wait 15–30 min.
