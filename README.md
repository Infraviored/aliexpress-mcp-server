# AliExpress MCP Server

An MCP server for shopping on AliExpress from an agent: search, compare offers
as checkout will price them, collect store coupons, and put a shopping list
into the cart, checked and ready. Prices, shipping and delivery are for the
configured country and currency (`ALIEXPRESS_COUNTRY`, `ALIEXPRESS_CURRENCY`,
default DE / EUR).

It never checks out or pays. It hands out the checkout link; paying is the
user's click.

## Tools

- `shop_list(items, apply)` — a shopping list to a ready cart. Finds the best
  offer per part (shipping per cart line for global sellers, Choice
  free-shipping threshold, store discounts, delivery time), and with
  `apply=True` adds/resizes the lines, removes its own replaced lines,
  collects the store coupon worth having, checks exactly the plan, returns
  the checkout link and compares plan and cart.
- `view_cart(checked_only)` — the cart as the web page shows it, grouped into
  "Shipped by AliExpress" (Choice) and global sellers, with what checkout
  would charge, a shipping analysis (every paid shipping line and why, the
  slowest line) and the other channel carts.
- `select_cart_items(cart_ids, only)` — check lines for checkout; returns the
  checkout link (AliExpress itself keeps at most 10 lines checked).
- `add_to_cart`, `update_cart_quantity`, `remove_from_cart` — cart lines.
- `best_offer(item_id, pieces)`, `plan_basket(lines)` — pricing without
  touching the cart.
- `get_discounts(item_id, subtotal)`, `collect_coupon(item_id, subtotal)` —
  store discounts and coupons; collecting runs the product page in Chrome.
- `search_products`, `get_product_details`, `get_product_variants`,
  `get_variant_images`, `get_product_reviews`, `get_shipping_estimate`.

The cart and coupon protocols were learned by recording the real pages with
`tools/capture_mtop.py`; see `docs/mtop-notes.md`. A bot check (slider or
reCAPTCHA box) is passed automatically in Chrome on a virtual display
(`slider.py`); cart lines this MCP added are remembered in
`~/.mcp-credentials/aliexpress-mcp-lines.json`.

## How it works

The initial product page is a client-side-rendered shell (`window._d_c_.isCSR = true`, `window.runParams = {}`) — real product data loads over AJAX from AliExpress's MTOP API after the JS runs. So HTML scraping doesn't work for PDP data.

Instead, this server replicates what `mtop.js` does in the browser: it makes signed calls to `mtop.aliexpress.pdp.pc.query` using the session's `_m_h5_tk` cookie as the HMAC token. The signing algorithm:

```
sign = md5(token + "&" + t_ms + "&" + "12574478" + "&" + json_payload)
```

where `token` is the prefix of `_m_h5_tk` before the underscore, and `12574478` is AliExpress's public web `appKey`. Token refresh on `FAIL_SYS_TOKEN_EXPIRED` is handled automatically.

Search still uses the simpler HTML path (embedded `window.runParams` JSON from the search SSR HTML) — no signed calls needed.

## Setup

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Install the [MCP Auth Bridge](https://github.com/justinritchie/mcp-auth-bridge) Chrome extension. The `aliexpress` entry is already in its `sites.json`.

3. Open `https://www.aliexpress.com`, log in, then click **Save AliExpress** in the extension popup. This writes cookies to `~/.mcp-credentials/aliexpress.json`.

4. Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

   ```json
   {
     "mcpServers": {
       "aliexpress": {
         "command": "python3",
         "args": ["/Users/YOUR_USER/justinritchie-mcp-servers/aliexpress-mcp-server/aliexpress_mcp_server.py"],
         "env": {
           "ALIEXPRESS_CREDENTIALS": "~/.mcp-credentials/aliexpress.json",
           "ALIEXPRESS_COUNTRY": "CA",
           "ALIEXPRESS_CURRENCY": "CAD"
         }
       }
     }
   }
   ```

5. Restart Claude Desktop.

## Known limitations

- **Low-volume / brand-new listings** sometimes return empty MTOP responses (endpoint returns `SUCCESS` but an empty data block). Likely a region/visibility gate. Search results still show the listing fine.
- **Cookies expire / lag the cart.** When tool calls start returning "session expired" — or `view_cart` reports an empty cart despite having items — re-open aliexpress.com in Chrome and click **Save AliExpress** again to capture fresh session cookies. The `_m_h5_tk` token and cart state rotate together; stale cookies show an empty server-side cart.
- **Rate limiting** is the user's responsibility. The server sends realistic Chrome headers but doesn't throttle; don't hammer the search.

## Architecture

Follows the pattern of [dekudeals-mcp-server](../dekudeals-mcp-server/) — FastMCP + httpx + BeautifulSoup — with added MTOP client code in `aliexpress_mcp_server.py`. Session cookies come from the shared credential file written by the [MCP Auth Bridge](https://github.com/justinritchie/mcp-auth-bridge) Chrome extension.

The MTOP response field paths (`PRODUCT_TITLE.text`, `PRICE.targetSkuPriceInfo.salePriceString`, `PC_RATING.rating`, `SHIPPING.originalLayoutResultList[0].bizData.displayAmount`, etc.) were reverse-engineered live from a real response in April 2026 and are documented inline in `_extract_pdp_fields()`. If AliExpress reorganizes the component layout, that function is the place to update.
