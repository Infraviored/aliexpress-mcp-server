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
next reload. Retested 2026-10-21 with the group checkbox
(`app_cart_group_title_component_ahe_global`, `checkbox.selected=true`,
`operationType: "checkbox"`): the response showed 13 checked, the next
render 10 — the three oldest were dropped. The limit is the server's, not a
paging artefact. Checkout is not limited: the checkout button opens

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

## Store coupons (2026-10-20)

**Read.** `mtop.aliexpress.pdp.promotion.panel` (see `_discounts`). Each
`shopCoupon` entry has `campaignId`, `collectText` ("Collect" / "Follow &
collect"), `collected`, `couponCode`, and `couponAssignParam.couponMeta`.
The panel marks private coupons (store welcome offer) as collected, public
code coupons never — the MCP remembers those in
`~/.mcp-credentials/aliexpress-coupons.json`.

**Collect.** The page calls `mtop.aliexpress.ump.coupon.common.assignNew`
(POST, `scenario: AssignSingleCoupon`, `couponMetaList: [couponMeta]`) with
browser fingerprints `ua` and `umidToken`. Without them the call answers
SUCCESS with empty success and fail lists and collects nothing. So
`browser_actions.collect_coupons` clicks it on the product page: open the
coupon entry `[class*=coupon-block--wrap]`, then the button
`[ae_button_type="Promotion_shopCoupon_collect"][ae_object_value*="activityId=<campaignId>;"]`
(language-independent), and reads the `assignNew` response.

**Product page extras seen, not used yet:** quantity price tiers ("3,15€ ≥10
Stücke") and an express shipping option.


## Checkout preview (renderOrder)

`mtop.aliexpress.checkout.renderOrder` v1.0, POST, works with the plain MTOP
signing (no browser, no umidToken needed). Data:

    {"ultronVersion": "3.0", "_saasRegion": "aeg", "pageType": "Shipping",
     "_currency": "EUR", "locale": "en_US",
     "extraParam": "{\"locale\":\"en_US\",\"intentional_currency\":\"EUR\",\"buyerCountry\":\"DE\",\"platformType\":\"PC\",…}",
     "shippingAddressId": "0",
     "buyParams": "{\"orderFrom\":\"SELECTIVE_CARTS\",\"items\":[{\"cartId\":\"…\"}]}",
     "contextID": "PlaceOrder_<uuid>"}

It renders the confirm page; nothing is ordered until "Pay now". The answer
is an ultron tree (`data.data`); `checkout.py` documents the blocks it reads.
The response holds the shipping address and the buyer id — never log it
whole (the test fixture is stripped of both).

`checkout_order_summary_TOTAL_SUMMARY.summaryLines[].type`: PRICE (goods),
DISCOUNTS, COUPON_CODE, SHIPPING_FEE, **DUTY_TAX_FEE** ("Estimated import
charges"), **TAX_DISCOUNT_FEE** ("AliExpress duty reduction"), DUTY_REMINDER.

**Import charges, measured 2026-10-21 (DE):**

- One line alone: 3.56–3.58 € whatever the goods (1.39 € Dupont set, 24.95 €
  of ESP boards) — the EU's flat 3 € per item plus 19 % VAT.
- Two Choice lines of the same kind (two microphones, two amplifiers): 7.14.
  Per line, not per tariff class, not per piece.
- Growing one Choice order line by line: 3.57 per line up to 9 lines
  (32.13); from the 10th line on the duty grows but the "duty reduction"
  grows by the same amount — net stays 32.13. Global seller lines added on
  top pay 3.57 each again (two global lines: 39.29).
