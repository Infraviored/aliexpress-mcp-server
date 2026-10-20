# MTOP notes — learned by recording the real pages

Recorded with `tools/capture_mtop.py` (real Chrome on Xvfb, the credential
cookies, every `acs.aliexpress.com` call logged). Dates are when it was seen.

## Cart (2026-10-20)

**Render.** `mtop.aliexpress.trade.cart.render` v1.0, GET, data:

    {"platformType": "DESKTOP", "_currency": "EUR", "shipToCountry": "DE",
     "_saasRegion": "aeg", "locale": "en_US",
     "bizParams": "{\"platformType\":\"DESKTOP\",\"pcChoiceNewCart\":1}"}

Without `bizParams`/`_currency` the API answers in an older format (USD,
different shipping fields, channel carts mixed in). That was the cause of the
wrong shipping (14.13 USD instead of 3.67 EUR) before.

The answer is a "droplet" snapshot: `{asyncHandler, config, page: {root,
structure}, data: {blockId: block}, global}`. Blocks carry `compName`; see
`cart.py` for the ones that matter. 10 lines per page.

**Actions.** `mtop.aliexpress.trade.cart.async` v1.0, POST, data = the render
payload plus `compress: true`, `nextPage`, and

    params = base64(gzip(json({config, operator, asyncHandler, page,
                               data: {operator: block, <root>: rootBlock}})))

`operator` is the id of the block that fired; the block's
`fields.operationType` says what happened:

| action | operator | change on the block |
|---|---|---|
| next page | `app_cart_pagination_container_page` | none; `nextPage: true` |
| check / uncheck | product block | `operationType: "checkbox"`, `checkbox.selected` |
| quantity | product block | `operationType: "update_quantity"`, `quantityView: {"current": n}` |
| delete | product block | `operationType: "delete"` |

**Send actions against the merged snapshot.** The page keeps one state over
all loaded pages: the newest `asyncHandler` (handler params + md5), the union
of all `triggers`, and every page's `page.structure` appended. An action sent
with a single page's snapshot makes AliExpress drop the selection of lines it
cannot see. `cart.merge` does what the page does.

**At most 10 checked lines are remembered.** Checking an 11th silently
unchecks the oldest one — the web page does the same; it shows 11 until the
next reload. Checkout is not limited: the checkout button opens

    https://www.aliexpress.com/p/trade/confirm.html?availableProductShopcartIds=<id>,<id>,…
        &aeOrderFrom=main_shopcart&intentionalPayMethod=&curPageLogUid=<ms>_<rand>&spm=a2g0o.cart.0.0

(built in the cart page's JS, `shoppingcart/1.0.68/index.js`), which renders
the order with exactly those lines via `mtop.aliexpress.checkout.renderOrder`
(`buyParams: {"orderFrom": "SELECTIVE_CARTS", "items": [{"cartId": …}]}`).
Nothing is bought until the user pays on that page. The MCP only hands out
the link.

**Channel carts.** `app_cart_head_component_cart_header.fields.businessCartInfo.businessCarts`
lists other carts (e.g. `nn_mix`, "Combo Blast cart") with their item count
and URL. Their lines are not part of this cart.

## Shipping per line (2026-10-20)

`logisticsView` on every product block: `freeShipping`, `freightCost`
("Shipping: 3,67€"), `chosenFreightService`, `deliveryDays`, and
`shippingUTParams` (JSON string) with `deliveryDateMin`, `deliveryDate`,
`deliveryDayMin/Max`, `shipFrom`. Global sellers pay shipping per line; two
variants of one product in two lines pay twice. Choice lines share one free
shipment above the threshold.
