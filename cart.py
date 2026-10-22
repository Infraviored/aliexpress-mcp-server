"""
The AliExpress cart as the web page shows it — parsed, grouped, explained.

`cart.render` / `cart.async` with `pcChoiceNewCart=1` return "droplet"
snapshots: {asyncHandler, config, page: {root, structure}, data: {blockId:
block}, global}. The blocks this module reads:

    app_cart_product_component_*      one cart line (itemView, priceViews,
                                      quantityView, checkbox, logisticsView,
                                      shopView)
    app_cart_group_title_component_*  "Shipped by AliExpress" (group_choice) /
                                      "Shipped by global sellers" (global),
                                      with cartLineIds over all pages
    app_cart_summary_component_*      what checkout would charge for the
                                      checked lines (subtotal, shipping,
                                      discounts, total)
    app_cart_head_component_*         line count, checked count, and the
                                      other "channel carts" (e.g. Combo Blast)

The cart arrives in pages (strategy "append"); `parse` takes every page.

Why shipping gets expensive (what `shipping_report` explains):
- Choice ("Shipped by AliExpress") ships free once the checked Choice lines
  together pass a threshold (about 10 €) — one shipment for all of them.
- Global sellers charge shipping per cart line. Two variants of one product,
  or two products from one store, are two lines and pay twice.
"""

import json
import re
from typing import Any, Optional

PRODUCT = "app_cart_product_component"
GROUP_TITLE = "app_cart_group_title_component"
SUMMARY = "app_cart_summary_component"
HEADER = "app_cart_head_component"
PAGINATION = "app_cart_pagination_container"

# AliExpress remembers at most this many checked lines; checking one more
# silently unchecks the oldest (the web page does the same, it only shows 11
# until reload). Checkout itself takes any number of lines by cart id.
MAX_CHECKED = 10

GROUP_NAMES = {"group_choice": "Shipped by AliExpress (Choice)", "global": "Shipped by global sellers"}


def _comp(block: dict) -> str:
    return block.get("compName") or "" if isinstance(block, dict) else ""


def _blocks(state: dict, comp: str) -> list[tuple[str, dict]]:
    return [(bid, b) for bid, b in (state.get("data") or {}).items() if _comp(b).startswith(comp)]


def cents(amount: Any) -> Optional[float]:
    if isinstance(amount, dict):
        cent = amount.get("cent")
        if isinstance(cent, (int, float)):
            return round(cent / 100.0, 2)
    return None


def money(text: Any) -> Optional[float]:
    """'Shipping: 3,67€' / '1.234,50 €' / '$4.02' / '-3,57€' / 'Free' -> float."""
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text or "").strip()
    m = re.search(r"\d[\d.,]*", s.replace(" ", ""))
    if not m:
        return 0.0 if "free" in s.lower() or "kostenlos" in s.lower() else None
    num = m.group(0)
    if "," in num and num.rfind(",") > num.rfind("."):
        num = num.replace(".", "").replace(",", ".")
    else:
        num = num.replace(",", "")
    try:
        v = float(num)
    except ValueError:
        return None
    return -v if s.startswith("-") else v


def cur_symbol(code: Optional[str]) -> str:
    return "€" if (code or "EUR") == "EUR" else f" {code}"


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html or "")).strip()


def pagination(state: dict) -> Optional[tuple[str, dict]]:
    found = _blocks(state, PAGINATION)
    return found[0] if found else None


def has_more(state: dict) -> bool:
    p = pagination(state)
    return bool(p and p[1].get("hasMore"))


def checkout_url(cart_ids: list[str]) -> str:
    """
    The order-confirmation page for exactly these cart lines — what the
    cart's checkout button opens. It shows the order; nothing is bought until
    the user pays there.
    """
    import random
    import time
    ids = ",".join(str(c) for c in cart_ids)
    uid = f"{int(time.time() * 1000)}_{random.randint(100000, 999999)}"
    return (f"https://www.aliexpress.com/p/trade/confirm.html?availableProductShopcartIds={ids}"
            f"&aeOrderFrom=main_shopcart&intentionalPayMethod=&curPageLogUid={uid}&spm=a2g0o.cart.0.0")


def merge(base: Optional[dict], new: dict) -> dict:
    """
    Fold a cart.async response into the running snapshot, the way the web page
    does: the newest handler state, the union of triggers, the page structure
    of every page appended, every block known. Actions must be sent against
    the merged snapshot; one built from a single page makes AliExpress drop
    the selection of lines it cannot see (a checkbox click on page 2 then
    unchecks some other line).
    """
    if base is None:
        return json.loads(json.dumps(new))
    out = json.loads(json.dumps(base))
    out["data"].update(json.loads(json.dumps(new.get("data") or {})))
    nh = new.get("asyncHandler") or {}
    oh = out.setdefault("asyncHandler", {})
    triggers = list(oh.get("triggers") or [])
    triggers += [t for t in nh.get("triggers") or [] if t not in triggers]
    out["asyncHandler"] = {**oh, **nh, "triggers": triggers}
    struct = out.setdefault("page", {}).setdefault("structure", {})
    for k, children in ((new.get("page") or {}).get("structure") or {}).items():
        # A line that moved (e.g. after a delete) lives under its new parent only.
        for other, kids in struct.items():
            if other != k:
                kids[:] = [c for c in kids if c not in children]
        have = struct.setdefault(k, [])
        have.extend(c for c in children if c not in have)
    for k in ("global", "config"):
        if new.get(k):
            out[k] = new[k]
    return out


def item_blocks(state: dict) -> dict[str, str]:
    """cartId (str) -> product block id, for the lines on this snapshot."""
    out = {}
    for bid, b in _blocks(state, PRODUCT):
        cid = ((b.get("fields") or {}).get("itemView") or {}).get("cartId")
        if cid is not None:
            out[str(cid)] = bid
    return out


def _line(fields: dict) -> dict:
    iv = fields.get("itemView") or {}
    lv = fields.get("logisticsView") or {}
    qv = fields.get("quantityView") or {}
    sv = fields.get("shopView") or {}
    price = None
    currency = None
    for pv in fields.get("priceViews") or []:
        price = cents(pv.get("amount"))
        currency = (pv.get("amount") or {}).get("currencyCode") or pv.get("currency")
        if price is not None:
            break
    try:
        ut = json.loads(lv.get("shippingUTParams") or "{}")
    except ValueError:
        ut = {}
    if lv.get("freeShipping"):
        ship = 0.0
    else:
        ship = money(lv.get("freightCost") or _strip(lv.get("freightDisplayText") or ""))
    qty = qv.get("current") or qv.get("origin") or 1
    return {
        "cart_id": str(iv.get("cartId")),
        "item_id": str(iv.get("itemId")),
        "sku_id": str(iv.get("skuId") or ""),
        "variant": (iv.get("sku") or {}).get("skuInfo") or "",
        "title": iv.get("title") or "",
        "image": iv.get("imageUrl"),
        "valid": bool(iv.get("valid", True)) and (iv.get("status") or "VALID") == "VALID",
        "checked": bool((fields.get("checkbox") or {}).get("selected")),
        "price": price,
        "currency": currency,
        "qty": int(qty),
        "store": sv.get("name") or "",
        "seller_id": str(sv.get("sellerId") or ""),
        "ship": ship,
        "delivery": (ut.get("deliveryDateMin"), ut.get("deliveryDate")),
        "days": (ut.get("deliveryDayMin"), ut.get("deliveryDayMax") or lv.get("deliveryDays")),
        "group": None,
        "choice": False,
    }


def parse(states: list[dict]) -> dict:
    """Every page snapshot of one cart walk -> one cart."""
    lines: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    header: dict = {}
    summary: dict = {}
    for state in states:
        for _, b in _blocks(state, PRODUCT):
            ln = _line(b.get("fields") or {})
            lines[ln["cart_id"]] = ln
        for _, b in _blocks(state, GROUP_TITLE):
            f = b.get("fields") or {}
            gid = f.get("id") or f.get("ceilingTag") or "?"
            promo = [_strip(t.get("text") or "") for t in ((f.get("groupPromotion") or {}).get("promotionTexts") or [])]
            groups[gid] = {
                "id": gid,
                "name": GROUP_NAMES.get(gid, _strip(f.get("title") or gid)),
                "cart_ids": [str(c) for c in f.get("cartLineIds") or []],
                "promo": [p for p in promo if p],
            }
        for _, b in _blocks(state, HEADER):
            f = b.get("fields") or {}
            channels = []
            for c in ((f.get("businessCartInfo") or {}).get("businessCarts") or []):
                n = re.search(r"(\d+)", _strip(c.get("subCartDesc") or ""))
                channels.append({
                    "name": c.get("cartDesc") or c.get("business"),
                    "business": c.get("business"),
                    "count": int(n.group(1)) if n else None,
                    "url": c.get("cartUrl"),
                })
            header = {"count": f.get("count"), "channels": channels}
        for _, b in _blocks(state, SUMMARY):
            summary = _summary(b.get("fields") or {})
    for g in groups.values():
        for cid in g["cart_ids"]:
            if cid in lines:
                lines[cid]["group"] = g["id"]
                lines[cid]["choice"] = g["id"] == "group_choice"
    return {"lines": list(lines.values()), "groups": groups, "header": header, "summary": summary}


def _summary(f: dict) -> dict:
    out: dict[str, Any] = {"currency": f.get("payCurrencyCode")}
    blocks = (f.get("summaryTabVO") or {}).get("priceBlockList") or []
    if not blocks:
        return out
    pb = blocks[0]
    out["total"] = cents(pb.get("total"))
    discounts = []
    for sl in pb.get("summaryLines") or []:
        kind = sl.get("type") or ""
        amount = cents(sl.get("amount"))
        if kind == "CART_ITEM_TOTAL":
            out["subtotal"] = amount
        elif kind == "CART_SHIPPING_FEE":
            out["shipping"] = amount
            sub = _strip((sl.get("subTitle") or {}).get("title") or "")
            if sub:
                out["shipping_note"] = sub
        elif "DISCOUNT" in kind or "COUPON" in kind or "PROMOTION" in kind:
            for ch in sl.get("childs") or [sl]:
                title = _strip((ch.get("title") or {}).get("title") or kind)
                discounts.append((title, cents(ch.get("amount"))))
    out["discounts"] = discounts
    return out


# ─── Explaining the cart ────────────────────────────────────────────────────

def goods(ln: dict) -> float:
    """Price × quantity of a cart line."""
    return (ln.get("price") or 0.0) * (ln.get("qty") or 1)


def by_list(cart: dict, tags: dict[str, dict]) -> tuple[dict[str, list], list]:
    """Lines per shopping list (`tags`: cart_id → {list, need, own}), and the rest."""
    lists: dict[str, list] = {}
    rest = []
    for ln in cart["lines"]:
        name = (tags.get(ln["cart_id"]) or {}).get("list")
        (lists.setdefault(name, []) if name else rest).append(ln)
    return lists, rest


def leftovers(listed: list, others: list) -> list[tuple[dict, dict]]:
    """(line, twin): lines of `others` naming the same part (model number) as a listed line."""
    known = {m: ln for ln in listed for m in models(ln["title"])}
    out = []
    for ln in others:
        twin = next((known[m] for m in models(ln["title"]) if m in known), None)
        if twin:
            out.append((ln, twin))
    return out


def shipping_report(cart: dict, choice_threshold: float = 10.0) -> dict:
    """
    For the checked lines: what shipping costs, why, and when it arrives.

    Returns {paid: [(line, reason)], slowest: line, latest: date,
    choice_goods, choice_short}.
    """
    checked = [ln for ln in cart["lines"] if ln["checked"]]
    paid = []
    seen_store: dict[str, dict] = {}
    seen_item: dict[str, dict] = {}
    for ln in checked:
        has_ship = ln.get("ship")  # 0.0 (free) is falsy; only truthy values are paid
        if has_ship:
            reasons = []
            if ln["choice"]:
                reasons.append("Choice below the free-shipping threshold")
            else:
                reasons.append("global seller: every cart line pays its own shipping")
                if ln["item_id"] in seen_item:
                    reasons.append(
                        f"same product already in another line "
                        f"({seen_item[ln['item_id']]['variant']}) — shipping paid twice"
                    )
                elif ln["seller_id"] and ln["seller_id"] in seen_store:
                    store_title = seen_store[ln["seller_id"]]["title"][:30]
                    reasons.append(f"same store as '{store_title}' — shipping paid twice")
                if ln["ship"] > goods(ln):
                    reasons.append(
                        f"shipping {ln['ship']:.2f} is more than the goods ({goods(ln):.2f})"
                    )
            paid.append((ln, "; ".join(reasons)))
        # Track every line (paid or free) so a paid line arriving after a free
        # line from the same store still triggers the duplicate-shipping note.
        if ln["seller_id"]:
            seen_store.setdefault(ln["seller_id"], ln)
        seen_item.setdefault(ln["item_id"], ln)
    dated = [ln for ln in checked if ln["delivery"][1]]
    slowest = max(dated, key=lambda ln: ln["delivery"][1]) if dated else None
    choice_goods = sum(goods(ln) for ln in checked if ln["choice"])
    return {
        "paid": paid,
        "slowest": slowest,
        "latest": slowest["delivery"][1] if slowest else None,
        "choice_goods": round(choice_goods, 2),
        "choice_short": round(choice_threshold - choice_goods, 2) if 0 < choice_goods < choice_threshold else 0.0,
    }


def _when(ln: dict) -> str:
    a, b = ln["delivery"]
    if not b:
        return "delivery ?"
    return f"{(a or b)[5:]}…{b[5:]}" if a and a != b else b[5:]


# Model numbers in a title ("PCM5102A", "MP1584EN", "ICS-43434"): letters
# and digits mixed, at least 4 long. Two lines sharing one are the same part.
_MODEL = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9][A-Z0-9-]{3,}\b")


def models(title: str) -> set[str]:
    return {m.replace("-", "") for m in _MODEL.findall(title or "")} - {"USB", "RGB"}


def _ship_s(ln: dict) -> str:
    if ln["ship"] == 0:
        return "free"
    if ln["ship"] is not None:
        return f"ship {ln['ship']:.2f}"
    return "Choice shipping" if ln["choice"] else "ship ?"


def _line_text(ln: dict, cur: str, prefix: str = "") -> str:
    qty = f"{ln['qty']}× " if ln["qty"] != 1 else ""
    bad = "" if ln["valid"] else " ⚠️ not available"
    kind = "Choice" if ln["choice"] else "global"
    return (f"- [{'x' if ln['checked'] else ' '}] {prefix}{qty}{ln['title'][:50]} | {ln['variant'] or '-'} | "
            f"{(ln['price'] or 0):.2f}{cur} | {_ship_s(ln)} | {kind} | {_when(ln)}{bad}\n"
            f"      cart_id {ln['cart_id']}")


def format_lists(cart: dict, tags: dict[str, dict]) -> tuple[list[str], set[str]]:
    """
    The lines on shopping lists (`tags`: cart_id → {list, need, own}), one
    section per list, each line with its need. Returns the text and the
    cart ids shown.
    """
    cur = cur_symbol(cart["summary"].get("currency"))
    lists, others = by_list(cart, tags)
    out, shown = [], set()
    for name, lines in lists.items():
        ticked = sum(1 for ln in lines if ln["checked"])
        value = sum(goods(ln) for ln in lines)
        out.append(f"## List '{name}' — {len(lines)} line(s), goods {value:.2f}{cur}; "
                   f"{ticked} ticked on the cart page")
        for ln in lines:
            t = tags[ln["cart_id"]]
            need = f"{t['need']}: " if t.get("need") else ""
            out.append(_line_text(ln, cur, need) + ("  (your own line)" if t.get("own") else ""))
            shown.add(ln["cart_id"])
        for ln, twin in leftovers(lines, others):
            out.append(f"   ! not on the list but the same part as '{tags[twin['cart_id']].get('need') or twin['title'][:30]}': "
                       f"{ln['title'][:45]} (cart_id {ln['cart_id']}) — a leftover?")
        out.append(f"   Checkout link for the whole list: {checkout_url([ln['cart_id'] for ln in lines])}")
        out.append("")
    return out, shown


def format_cart(cart: dict, show_unchecked: bool = True, tags: Optional[dict[str, dict]] = None) -> str:
    cur = cur_symbol(cart["summary"].get("currency"))
    h = cart["header"]
    n_checked = sum(1 for ln in cart["lines"] if ln["checked"])
    out = [f"Cart: {len(cart['lines'])} line(s), {n_checked} ticked on the cart page."]
    listed, shown = format_lists(cart, tags or {}) if tags else ([], set())
    if listed:
        out += [""] + listed
        out.append("## Other lines (not on a list)" if len(shown) < len(cart["lines"]) else "")
    grouped: dict[str, list] = {}
    for ln in cart["lines"]:
        if ln["cart_id"] in shown:
            continue
        grouped.setdefault(ln["group"] or "other", []).append(ln)
    order = [g for g in ("group_choice", "global") if g in grouped] + [g for g in grouped if g not in ("group_choice", "global")]
    for gid in order:
        lines = grouped[gid]
        g = cart["groups"].get(gid, {"name": gid, "promo": []})
        ch = [ln for ln in lines if ln["checked"]]
        value = sum(goods(ln) for ln in ch)
        ship = sum(ln["ship"] or 0 for ln in ch)
        out.append("")
        out.append(f"{'###' if listed else '##'} {g['name']} — {len(ch)}/{len(lines)} ticked, "
                   f"goods {value:.2f}{cur}, shipping {ship:.2f}{cur}")
        for p in g.get("promo") or []:
            out.append(f"   ({p})")
        for ln in sorted(lines, key=lambda x: not x["checked"]):
            if not ln["checked"] and not show_unchecked:
                continue
            out.append(_line_text(ln, cur) + f"  item {ln['item_id']}  sku {ln['sku_id']}  {ln['store'][:25]}")
    s = cart["summary"]
    if s.get("total") is not None:
        out.append("")
        out.append(f"## Cart page total (only the {n_checked} ticked lines; no import charges — "
                   "checkout_preview shows what checkout really charges)")
        out.append(f"Subtotal {s.get('subtotal') or 0:.2f}{cur} + shipping {s.get('shipping') or 0:.2f}{cur}"
                   + (f" ({s['shipping_note']})" if s.get("shipping_note") else ""))
        for title, amt in s.get("discounts") or []:
            if amt:
                out.append(f"  − {title} {amt:.2f}{cur}")
        out.append(f"Total {s['total']:.2f}{cur}")
    r = shipping_report(cart)
    out.append("")
    out.append("## Shipping analysis")
    if r["paid"]:
        for ln, why in r["paid"]:
            out.append(f"- {ln['ship']:.2f}{cur} {ln['title'][:40]} ({ln['variant']}): {why}")
    else:
        out.append("- no paid shipping on the checked lines")
    if r["choice_short"]:
        out.append(f"- Choice lines total {r['choice_goods']:.2f}{cur}: {r['choice_short']:.2f}{cur} short of free shipping")
    if r["slowest"]:
        sl = r["slowest"]
        out.append(f"- slowest: {sl['title'][:40]} ({sl['store'][:25]}), arrives by {r['latest']}")
    for c in h.get("channels") or []:
        out.append(f"- other cart '{c['name']}': {c['count']} item(s), not in this cart ({c['url']})")
    ids = [ln["cart_id"] for ln in cart["lines"] if ln["checked"]]
    if ids:
        out.append("")
        out.append(f"Checkout link for the {len(ids)} ticked line(s): {checkout_url(ids)}")
    return "\n".join(out)
