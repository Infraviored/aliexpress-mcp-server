# AliExpress MCP Server

An MCP server for shopping on AliExpress from an agent: search, compare offers
as checkout will price them, and turn a shopping list into a cart that is ready
to order. Prices, shipping and delivery are for `ALIEXPRESS_COUNTRY` /
`ALIEXPRESS_CURRENCY` (default DE / EUR).

It never checks out or pays. It hands out the checkout link, and paying is the
user's click. `SKILL.md` is the guide for the agent.

## Tools

- `shop_list(items, list_name, apply)` takes a shopping list and returns a
  ready cart. For each part it finds the best offer, counting shipping and
  import charges per cart line, the Choice free-shipping threshold, store
  discounts and delivery time. With `apply=True` it puts exactly that plan
  into the cart and returns the checkout link.
- `view_cart` shows the cart per shopping list. It includes the real checkout
  total and the decisions still open, and writes the overview page
  `cart.html`.
- `checkout_preview(cart_ids | list_name)` reads the real checkout page for
  any set of lines. Nothing is bought.
- `set_cart_list`, `select_cart_items`, `add_to_cart`, `update_cart_quantity`
  and `remove_from_cart` change cart lines.
- `best_offer(item_id, pieces)` finds the cheapest pack mix from one product.
- `get_discounts` and `collect_coupon` handle store discounts and coupons.
- `search_products`, `get_product_details`, `get_product_variants`,
  `get_variant_images`, `get_product_reviews` and `get_shipping_estimate`
  look up products.

## How it works

Product, cart and checkout data come from AliExpress's MTOP API, the same
signed calls the web pages make:

```
sign = md5(token + "&" + t_ms + "&" + "12574478" + "&" + json_payload)
```

Here `token` is the part of the `_m_h5_tk` cookie before the underscore. An
expired token is refreshed and saved back to the credential file. The cart and
checkout protocols were recorded from the real pages with
`tools/capture_mtop.py`; see `docs/mtop-notes.md`. Search parses the search
page's HTML.

Calls are paced (`ALIEXPRESS_MTOP_GAP`, 0.8 s) and product pages are cached
for 10 min. A bot check (a slider or a reCAPTCHA box) is passed automatically
in Chrome on a virtual display (`slider.py`).

Local state lives next to the credentials in `~/.mcp-credentials/`, all files
mode 600:
- the cart lines this MCP added and the shopping list each line belongs to;
- the coupons it collected;
- cached Choice alternatives.

Import charges: the EU charges a flat 3 € duty per item in small parcels.
AliExpress collects it at checkout as about 3.57 € per cart line and takes over
part of it on larger Choice orders. The planner estimates it per line, and
`checkout_preview` reads the real figure.

## Setup

1. Install the dependencies and Chrome for Playwright:

   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   .venv/bin/playwright install chrome
   ```

2. Log in on aliexpress.com. Then export the cookies (for example with the
   [MCP Auth Bridge](https://github.com/justinritchie/mcp-auth-bridge)
   extension) and import them:

   ```bash
   .venv/bin/python import_cookies.py cookies.txt   # writes ~/.mcp-credentials/aliexpress.json
   ```

3. Register the server with your MCP client. `mcp_config_example.json` shows
   the entry; set `ALIEXPRESS_COUNTRY` and `ALIEXPRESS_CURRENCY` there.

## Tests

```bash
.venv/bin/python -m pytest -q tests
```

The fixtures are recorded responses with personal data removed.

## Known limitations

- **Empty product data:** very new or low-volume listings sometimes return an
  empty product response.
- **Expired cookies:** when the cookies expire, tools say "session expired" or
  the cart looks empty. Log in again and re-import the cookies.
