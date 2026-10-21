"""
What checkout would charge — read from the real checkout page, nothing bought.

`mtop.aliexpress.checkout.renderOrder` renders the order page for any set of
cart lines (`buyParams: {"orderFrom": "SELECTIVE_CARTS", "items": [{"cartId"}]}`).
It is what the confirm page loads before "Pay now"; rendering places no order.
Unlike the cart page it is not limited to 10 checked lines, and it is the only
place that shows the import charges.

Blocks this module reads (response `data.data`):

    checkout_order_summary_TOTAL_SUMMARY   summaryLines by type: PRICE (subtotal),
                                           DISCOUNTS, SHIPPING_FEE, DUTY_TAX_FEE
                                           (estimated import charges),
                                           TAX_DISCOUNT_FEE (AliExpress duty reduction)
    checkout_order_total_*                 preOrderTotal {cent, currencyCode}
    pc_checkout_product_<sig>              a global seller's line (cartId, itemId,
                                           quantity, title, valid)
    pc_checkout_group_product_*_choice*    the Choice lines (intentionOrderList)
    pc_checkout_shipping_option_<sig>      chosen shipping of the line with that
                                           signature (cost, service, delivery date)
    checkout_shop_title_*                  store name, `signatures` of its lines

Import charges (measured 2026-10-21, ship to DE): 3.57 € per cart line — the
EU flat 3 € customs duty per item plus 19 % VAT — whatever the quantity or
price of the line. Two kinds of microphone are two lines and pay twice.
For "Shipped by AliExpress" (Choice) lines AliExpress pays the charges beyond
the 9th line ("AliExpress duty reduction"): 9 Choice lines cost 32.13, the
10th, 11th, 12th nothing more. Every global seller's line pays its 3.57.
"""

import re
from typing import Any, Optional

# Per cart line, ship to DE (3 € EU duty + 19 % VAT). Measured, see above.
DUTY_PER_LINE = 3.57
# Choice lines beyond this many carry no import charges (AliExpress pays).
CHOICE_DUTY_LINES = 9


def duty_for(choice_lines: int, global_lines: int, per_line: float = DUTY_PER_LINE) -> float:
    """Import charges of an order: Choice lines capped at CHOICE_DUTY_LINES, global lines each."""
    return round(per_line * (min(choice_lines, CHOICE_DUTY_LINES) + global_lines), 2)


def _money(text: Any) -> Optional[float]:
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    if "free" in s.lower() or "kostenlos" in s.lower():
        return 0.0
    m = re.search(r"-?\d[\d.,]*", s.replace(" ", ""))
    if not m:
        return None
    num = m.group(0)
    # 1.234,56 / 1,234.56 / 3,57 / 3.57
    if "," in num and "." in num:
        num = num.replace(".", "").replace(",", ".") if num.rfind(",") > num.rfind(".") else num.replace(",", "")
    else:
        num = num.replace(",", ".")
    try:
        v = float(num)
    except ValueError:
        return None
    return -abs(v) if s.startswith("-") else v


def _qty(q: Any) -> int:
    if isinstance(q, dict):
        q = q.get("current")
    try:
        return int(q or 1)
    except (TypeError, ValueError):
        return 1


def _price(prices: Any) -> Optional[float]:
    for key, p in ((prices or {}).get("children") or {}).items():
        amount = (p or {}).get("amount") or {}
        if amount.get("cent") is not None:
            return amount["cent"] / 100
    return None


def parse(resp: dict) -> dict:
    """
    {subtotal, discounts, shipping, duty, duty_off, total, currency,
     lines: [{cart_id, item_id, title, qty, price, choice, store, ship, service,
              delivery, valid}], latest, slowest}
    """
    data = ((resp.get("data") or {}).get("data")) or {}
    out: dict[str, Any] = {"subtotal": None, "discounts": 0.0, "shipping": None, "duty": 0.0,
                           "duty_off": 0.0, "total": None, "currency": "EUR", "lines": []}
    ship_by_sig: dict[str, dict] = {}
    store_by_sig: dict[str, str] = {}
    for bid, b in data.items():
        f = b.get("fields") or {}
        if bid.startswith("checkout_order_summary_TOTAL_SUMMARY"):
            for ln in f.get("summaryLines") or []:
                amount = _money((ln.get("content") or {}).get("content"))
                kind = ln.get("type")
                if kind == "PRICE":
                    out["subtotal"] = amount
                elif kind == "DISCOUNTS" and amount is not None:
                    out["discounts"] = abs(amount)
                elif kind == "SHIPPING_FEE":
                    out["shipping"] = amount if amount is not None else 0.0
                elif kind == "DUTY_TAX_FEE" and amount is not None:
                    out["duty"] = amount
                elif kind == "TAX_DISCOUNT_FEE" and amount is not None:
                    out["duty_off"] = abs(amount)
        elif bid.startswith("checkout_order_total"):
            t = f.get("preOrderTotal") or {}
            if t.get("cent") is not None:
                out["total"] = t["cent"] / 100
                out["currency"] = t.get("currencyCode") or out["currency"]
        elif bid.startswith("pc_checkout_shipping_option_"):
            s = f.get("selectedFreightService") or {}
            ship_by_sig[bid.rsplit("_", 1)[1]] = {
                "ship": _money(s.get("shippingCostContent") or s.get("freightCost")),
                "service": s.get("displayName") or s.get("serviceName") or "",
                "delivery": s.get("deliveryDate"),
                "days": s.get("deliveryDays"),
            }
        elif bid.startswith("checkout_shop_title_"):
            for sig in f.get("signatures") or []:
                store_by_sig[sig] = f.get("title") or ""
    lines = []
    for bid, b in data.items():
        f = b.get("fields") or {}
        if bid.startswith("pc_checkout_product_"):
            lines.append((f, False))
        elif bid.startswith("pc_checkout_group_product") and "choice" in bid:
            lines += [(x, True) for x in f.get("intentionOrderList") or []]
    for f, choice in lines:
        sig = f.get("signature") or ""
        sh = ship_by_sig.get(sig, {})
        store = store_by_sig.get(sig, "")
        if not store:
            for tag in ((f.get("expressTextTags") or {}).get("children") or []):
                if tag.get("text") and "VAT" not in tag["text"]:
                    store = tag["text"]
        out["lines"].append({
            "cart_id": str(f.get("cartIdStr") or f.get("cartId") or ""),
            "item_id": str(f.get("itemId") or ""),
            "title": f.get("title") or "",
            "qty": _qty(f.get("quantity")),
            "price": _price(f.get("prices")),
            "choice": choice,
            "store": store,
            # Choice lines ship together: their own option says "Free", the
            # group's fee is in the SHIPPING_FEE total.
            "ship": sh.get("ship"),
            "service": sh.get("service", ""),
            "delivery": sh.get("delivery"),
            "valid": f.get("valid", True) is not False and f.get("status", "VALID") == "VALID",
        })
    dated = [ln for ln in out["lines"] if ln["delivery"]]
    out["slowest"] = max(dated, key=lambda ln: ln["delivery"]) if dated else None
    out["latest"] = out["slowest"]["delivery"] if out["slowest"] else None
    return out


def format_preview(p: dict, asked: int, url: str) -> str:
    cur = "€" if p["currency"] == "EUR" else f" {p['currency']}"
    n = len(p["lines"])
    out = [f"Checkout for {n} line(s) — as the checkout page shows it (nothing bought):"]
    if asked and n != asked:
        out.append(f"! {asked} cart id(s) asked, checkout took {n} — the others are gone or not orderable")
    out.append(f"  goods            {p['subtotal'] or 0:8.2f}{cur}")
    if p["discounts"]:
        out.append(f"  discounts        {-p['discounts']:8.2f}{cur}")
    out.append(f"  shipping         {p['shipping'] or 0:8.2f}{cur}")
    if p["duty"]:
        out.append(f"  import charges   {p['duty']:8.2f}{cur}  (EU 3 € duty + VAT per cart line)")
    if p["duty_off"]:
        out.append(f"  duty reduction   {-p['duty_off']:8.2f}{cur}")
    if p["total"] is not None:
        out.append(f"  TOTAL            {p['total']:8.2f}{cur}")
    paid = [ln for ln in p["lines"] if ln["ship"]]
    for ln in paid:
        out.append(f"- shipping {ln['ship']:.2f}{cur}: {ln['title'][:45]} ({ln['store'][:25]}, {ln['service']})")
    for ln in p["lines"]:
        if not ln["valid"]:
            out.append(f"- ⚠️ not orderable: {ln['title'][:50]}")
    if p["slowest"]:
        out.append(f"- slowest: {p['slowest']['title'][:45]}, arrives by {p['latest']}")
    if p["duty"] and n:
        n_choice = sum(1 for ln in p["lines"] if ln["choice"])
        n_global = n - n_choice
        tip = (f"- import charges: {DUTY_PER_LINE:.2f}{cur} per cart line whatever its price or quantity; "
               f"Choice lines only up to {CHOICE_DUTY_LINES} (here {n_choice} Choice")
        tip += (f", so {n_choice - CHOICE_DUTY_LINES} of them free" if n_choice > CHOICE_DUTY_LINES else "")
        tip += f"), every global seller line pays ({n_global} here)"
        out.append(tip)
        if n_global and n_choice >= CHOICE_DUTY_LINES:
            out.append(f"  → a global line moved to a Choice offer saves its {DUTY_PER_LINE:.2f}{cur} "
                       "import charges and its own shipping")
    out.append(f"Checkout link: {url}")
    return "\n".join(out)
