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
- "Bot check not passed": stop calling for 15–30 min; every call extends it.
