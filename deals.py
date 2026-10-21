"""
Deal arithmetic for AliExpress offers — done here, so tools return a verdict
instead of long variant lists the model has to compute over.

Three facts this encodes (learned the hard way):

1. A variant property ("Color", "Farbe", ...) is often a pack size ("1Pcs",
   "5Pcs", "10Pcs"), sometimes a different article. Packs of the same base
   variant form a ladder, and the next rung is often nearly free
   (1 pc 0.32 €, 5 pcs 1.39 €).
2. Shipping for non-Choice offers grows with quantity, roughly A + B·qty
   (ESP32 board: 3.28, 3.49, 4.13, 5.21 € for 1, 2, 5, 10). Measured with
   the shipping-panel API at qty 1 and 2. It is charged per cart line:
   two pack sizes of one product are two lines and pay shipping twice
   (cart 2026-10-20: 2×1PCS 3.67 € + 1×3PCS 3.66 €).
3. Choice items ship free once the Choice part of the order passes a
   threshold (10 € in DE); below it a flat fee applies.
"""

import itertools
import json
import math
import re
from typing import Any, Callable, Optional

PACK_RE = re.compile(r"(?<![\d.])(\d{1,4})\s*(?:pcs|pc|pieces?|stück|stk|pack|lots?)\b|pack of\s*(\d{1,4})", re.I)


def pack_size(label: str) -> int:
    m = PACK_RE.search(label or "")
    if not m:
        return 1
    n = int(m.group(1) or m.group(2))
    return n if 0 < n <= 1000 else 1


def base_name(label: str) -> str:
    """Variant name with the pack size removed: '5PCS / Blue' -> '/ blue'."""
    return re.sub(r"\s+", " ", PACK_RE.sub("", label or "")).strip(" /-").lower()


def ladders(variants: list[dict]) -> dict[str, list[dict]]:
    """Group salable, priced variants by base name; keep groups with >1 pack size."""
    groups: dict[str, list[dict]] = {}
    for v in variants:
        if v.get("salable", True) and v.get("price") is not None:
            groups.setdefault(base_name(v["label"]), []).append(v)
    out = {}
    for k, vs in groups.items():
        sizes = {v["pack"] for v in vs}
        if len(sizes) > 1:
            # one variant per pack size: the cheapest
            best: dict[int, dict] = {}
            for v in vs:
                if v["pack"] not in best or v["price"] < best[v["pack"]]["price"]:
                    best[v["pack"]] = v
            out[k] = [best[p] for p in sorted(best)]
    return out


# ─── Shipping as A + B·qty ──────────────────────────────────────────────────

def freight_quote(
    mtop_call: Callable[..., dict],
    item_id: str,
    country: str,
    currency: str,
    delivery_ext: Optional[str],
    referer: str,
    sku_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Ask the shipping panel for qty 1 and 2 and fit fee(qty) = A + B·(qty-1).
    Returns {"A", "B", "company", "days", "free", "estimate": True} in `currency`.
    The panel prices per item, not per variant; treat it as an estimate.
    """
    ext = delivery_ext
    if ext and sku_id:
        try:
            e = json.loads(ext)
            e = e[0] if isinstance(e, list) else e
            e["p0"] = str(sku_id)
            ext = json.dumps(e)
        except (ValueError, TypeError, KeyError, IndexError):
            pass

    def ask(qty: int) -> Optional[dict]:
        data = {
            "productId": item_id, "country": country, "provinceCode": "", "province": "",
            "cityCode": "", "city": "", "lang": "en_US", "locale": "en_US", "_lang": "en_US",
            "tradeCurrency": currency, "_currency": currency, "quantity": qty,
            "clientType": "pc", "userScene": "PC_DETAIL_SHIPPING_PANEL",
        }
        if ext:
            data["ext"] = ext
        resp = mtop_call(
            "mtop.global.expression.dynamic.component.queryOptionForItem", "1.0", data,
            referer=referer, method="POST",
        )
        lays = ((resp.get("data") or {}).get("originalLayoutResultList")) or []
        return (lays[0].get("bizData") or {}) if lays else None

    one = ask(1)
    if not one:
        return None
    two = ask(2) or one
    fee1 = 0.0 if one.get("shippingFee") == "free" else float(one.get("displayAmount") or 0)
    fee2 = 0.0 if two.get("shippingFee") == "free" else float(two.get("displayAmount") or fee1)
    return {
        "A": fee1,
        "B": max(0.0, round(fee2 - fee1, 2)),
        "free": one.get("shippingFee") == "free",
        "company": one.get("company") or one.get("deliveryProviderName"),
        "days": (one.get("deliveryDayMin"), one.get("deliveryDayMax")),
        "currency": one.get("displayCurrency") or currency,
    }


def shipping_for(quote: Optional[dict], qty: int) -> float:
    if not quote or quote.get("free") or qty < 1:
        return 0.0
    return quote["A"] + quote["B"] * (qty - 1)


def fmt_quote(quote: Optional[dict], currency: str) -> str:
    if not quote:
        return "shipping: unknown"
    if quote.get("free"):
        return "shipping: free"
    extra = f" + {quote['B']:.2f}/extra unit" if quote["B"] else " (flat)"
    days = quote.get("days") or (None, None)
    return f"shipping: {quote['A']:.2f} {currency}{extra}" + (f", {days[0]}–{days[1]} days" if days[1] else "")


# ─── Cheapest way to N pieces ───────────────────────────────────────────────

def best_ways(
    variants: list[dict],
    need: int,
    quote: Optional[dict],
    choice_free: bool,
    limit: int = 3,
    duty: float = 0.0,
) -> list[dict]:
    """
    All ways to get >= `need` pieces from one ladder, using up to two pack
    sizes, ranked by total (goods + shipping and import charges per cart
    line), then by fewer lines. Each way: {"lines": [(variant, qty)],
    "line_ship": [fee per line], "pieces", "goods", "ship", "duty", "total"}.
    `choice_free`: shipping counts as 0 (Choice item in an order above threshold).
    `duty`: import charges per cart line (EU: flat per line, not per piece).
    """
    ways = []
    groups = ladders(variants) or {}
    # variants without a ladder are their own one-rung ladders
    for v in variants:
        if v.get("salable", True) and v.get("price") is not None:
            groups.setdefault(base_name(v["label"]) + f"#{v['sku_id']}", [v])
    seen = set()
    for rungs in groups.values():
        for a, b in itertools.combinations_with_replacement(rungs, 2):
            for qa in range(0, math.ceil(need / a["pack"]) + 1):
                rest = need - qa * a["pack"]
                qb = 0 if rest <= 0 else (math.ceil(rest / b["pack"]) if b is not a else 0)
                if b is a and rest > 0:
                    continue
                if qa == 0 and qb == 0:
                    continue
                lines = tuple((v["sku_id"], q) for v, q in ((a, qa), (b, qb)) if q)
                if lines in seen:
                    continue
                seen.add(lines)
                pieces = qa * a["pack"] + qb * b["pack"]
                goods = qa * a["price"] + qb * b["price"]
                used = [(v, q) for v, q in ((a, qa), (b, qb)) if q]
                # one cart line per variant, each with its own shipping
                line_ship = [0.0 if choice_free else shipping_for(quote, q) for _, q in used]
                ship = sum(line_ship)
                tax = duty * len(used)
                ways.append({
                    "lines": used, "line_ship": line_ship,
                    "pieces": pieces, "goods": goods, "ship": ship, "duty": tax,
                    "total": goods + ship + tax,
                })
    ways.sort(key=lambda w: (w["total"], len(w["lines"]), -w["pieces"]))
    return ways[:limit]


def odd_one_out(ways: list[dict], variants: list[dict], ratio: float = 0.75) -> Optional[str]:
    """
    Warn when the winning way uses a variant far cheaper per piece than every
    other kind of variant: usually a different article ("Chip" = bare module
    vs. "Module" = dev board, "expansion board", a cable), not a bargain.
    """
    if not ways:
        return None
    win = {base_name(v["label"]) for v, _ in ways[0]["lines"]}
    unit = lambda v: v["price"] / v["pack"]
    mine = min(unit(v) for v, _ in ways[0]["lines"])
    others = [unit(v) for v in variants
              if v.get("price") is not None and base_name(v["label"]) not in win]
    if others and mine < ratio * min(others):
        names = ", ".join(sorted({v["label"] for v, _ in ways[0]["lines"]}))
        return (f"! '{names}' costs under {ratio:.0%} of every other variant per piece — "
                "probably a different article (bare chip, accessory). Check get_variant_images, "
                "then pass match= for the one you want.")
    return None


def fmt_way(w: dict, currency: str) -> str:
    parts = " + ".join(f"{q}× {v['label']}" for v, q in w["lines"])
    ship = f" + {w['ship']:.2f} ship" if w["ship"] else ""
    tax = f" + {w['duty']:.2f} import" if w.get("duty") else ""
    return f"{parts} = {w['pieces']} pcs, {w['goods']:.2f}{ship}{tax} = {w['total']:.2f} {currency} ({w['total'] / w['pieces']:.2f}/pc)"


def ladder_lines(variants: list[dict], currency: str) -> list[str]:
    """
    Always one compact line per pack ladder, plus a warning where a bigger
    pack costs more per piece: '1Pcs 0.32 | 5Pcs 1.39 (0.28/pc, +1.07) | 10Pcs 2.79 (0.28/pc, +1.40)'.
    """
    out = []
    for rungs in ladders(variants).values():
        parts = []
        for i, v in enumerate(rungs):
            s = f"{v['label']} {v['price']:.2f}"
            if v["pack"] > 1:
                s += f" ({v['price'] / v['pack']:.2f}/pc"
                s += f", +{v['price'] - rungs[i - 1]['price']:.2f})" if i else ")"
            parts.append(s)
        out.append(f"pack ladder ({currency}): " + " | ".join(parts))
        for lo, hi in zip(rungs, rungs[1:]):
            if hi["price"] / hi["pack"] > lo["price"] / lo["pack"] * 1.05:
                out.append(f"  ! {hi['label']} costs more per piece than {lo['label']} — buy several {lo['label']} instead")
    return out


# ─── Variant pictures as one contact sheet ──────────────────────────────────

def contact_sheet(rows: list[dict], fetch: Callable[[str], Optional[bytes]], currency: str,
                  tile: int = 220, cols: int = 4, max_tiles: int = 16) -> Optional[bytes]:
    """
    One JPEG grid: each variant's picture with '#n', name and price under it.
    `rows`: dicts with label, price, image (URL). Returns JPEG bytes, or None
    when no variant has a picture.
    """
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont

    rows = [r for r in rows if r.get("image")][:max_tiles]
    if not rows:
        return None
    label_h = 44
    n = len(rows)
    cols = min(cols, n)
    nrows = math.ceil(n / cols)
    sheet = Image.new("RGB", (cols * tile, nrows * (tile + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:
        font = ImageFont.load_default()
    for i, r in enumerate(rows):
        x, y = (i % cols) * tile, (i // cols) * (tile + label_h)
        data = fetch(r["image"])
        if data:
            try:
                im = Image.open(BytesIO(data)).convert("RGB")
                im.thumbnail((tile - 8, tile - 8))
                sheet.paste(im, (x + (tile - im.width) // 2, y + (tile - im.height) // 2))
            except Exception:
                pass
        draw.rectangle([x, y, x + tile - 1, y + tile + label_h - 1], outline="#cccccc")
        draw.text((x + 6, y + 4), f"#{r['n']}", fill="#d0021b", font=font)
        name = r["label"] if len(r["label"]) <= 30 else r["label"][:29] + "…"
        draw.text((x + 6, y + tile + 2), name, fill="black", font=font)
        price = f"{r['price']:.2f} {currency}" if r.get("price") is not None else ""
        draw.text((x + 6, y + tile + 22), price, fill="#333333", font=font)
    buf = BytesIO()
    sheet.save(buf, "JPEG", quality=80, optimize=True)
    return buf.getvalue()


def upsell(ways: list[dict], currency: str, max_extra: float = 1.5, max_ratio: float = 0.35) -> Optional[str]:
    """
    'More for little': the cheapest way that gives at least twice the pieces
    of the best way for a small extra (≤ max_extra or ≤ max_ratio of the total).
    """
    if not ways:
        return None
    best = ways[0]
    bigger = [w for w in ways[1:] if w["pieces"] >= 2 * best["pieces"]]
    if not bigger:
        return None
    w = min(bigger, key=lambda x: x["total"])
    extra = w["total"] - best["total"]
    if extra <= max(max_extra, max_ratio * best["total"]):
        return f"tip: +{extra:.2f} {currency} gets {w['pieces']} pcs instead of {best['pieces']} — " + fmt_way(w, currency)
    return None


# ─── Store discounts, coupons, coins (pdp.promotion.panel) ──────────────────

_NUM = r"(\d+(?:[.,]\d+)?)"


def _money(text: str) -> Optional[float]:
    m = re.search(_NUM, text or "")
    return float(m.group(1).replace(",", ".")) if m else None


def parse_discounts(resp: dict) -> dict:
    """
    The "Discounts & coupon codes" panel as data:
    {"seller_id", "tiers": [(min, off)] store discount (automatic, best tier
    applies), "coupons": [{off, min, how, code, campaign_id, label}] (one per
    order per store; how = collected / collect / follow & collect / code /
    automatic), "coins_pct", "coins", "other": [text]}.
    """
    out = {"seller_id": None, "tiers": [], "coupons": [], "coins_pct": 0.0, "coins": None, "other": []}
    raw = ((resp.get("data") or {}).get("result") or {}).get("panelPageGroupResultStr")
    if not raw:
        return out
    try:
        groups = json.loads(raw).get("panelPageGroupInfoList") or []
    except ValueError:
        return out
    for g in groups:
        for p in g.get("panelPagePromotionList") or []:
            tips = {r.get("key"): r.get("tipList") or [] for r in p.get("resourcePositions") or []}
            title, sub, desc = tips.get("title", []), tips.get("sub_title", []), tips.get("description", [])
            kind = p.get("displayType") or ""
            out["seller_id"] = out["seller_id"] or p.get("sellerId")
            if kind == "flexiCoin":
                pct = re.search(r"(\d+(?:[.,]\d+)?)\s*%", " ".join(title))
                out["coins_pct"] = float(pct.group(1).replace(",", ".")) if pct else 0.0
                have = re.search(r"(\d+)\s*coins", " ".join(sub))
                out["coins"] = int(have.group(1)) if have else None
            elif kind == "fixedDiscount":
                for t in title:  # "Get 0,91€ off orders over 18,25€"
                    m = re.search(_NUM + r"\D+off\D+over\D*" + _NUM, t)
                    if m:
                        out["tiers"].append((float(m.group(2).replace(",", ".")), float(m.group(1).replace(",", "."))))
            elif title and _money(title[0]) is not None and "off" in title[0].lower() and "%" not in title[0]:
                collect = (p.get("collectText") or "").lower()
                assign = p.get("couponAssignParam") or {}
                # "Follow & collect" would follow the store. Seen as bizType 121 /
                # sellerCouponActivityType 21; the text is only the fallback,
                # since it follows the page language.
                follows = (p.get("bizType") == 121 or assign.get("sellerCouponActivityType") == 21
                           or "follow" in collect or "folgen" in collect)
                if p.get("collected"):
                    how = "collected"
                elif follows:
                    how = "follow & collect"
                elif collect:
                    how = "collect"
                elif p.get("couponCode"):
                    how = "code"
                else:
                    how = "automatic"
                out["coupons"].append({
                    "off": _money(title[0]), "min": _money(" ".join(sub)) or 0.0,
                    "how": how, "label": " ".join(desc) or kind,
                    "code": p.get("couponCode"), "campaign_id": str(p.get("campaignId") or ""),
                })
            else:
                text = " · ".join(title + sub + desc)
                if text:
                    out["other"].append(text)
    out["tiers"].sort()
    out["coupons"].sort(key=lambda c: (c["min"], -c["off"]))
    return out


def discount_for(d: Optional[dict], subtotal: float, coupons: bool = True) -> tuple[float, str]:
    """
    Best saving for a store subtotal: best store-discount tier (automatic) +
    best single coupon. `coupons=False`: only what applies without anyone
    collecting or typing anything (automatic, or already collected).
    """
    if not d:
        return 0.0, ""
    parts, save = [], 0.0
    tier = max((t for t in d["tiers"] if t[0] <= subtotal), default=None, key=lambda t: t[1])
    if tier:
        save += tier[1]
        parts.append(f"store discount {tier[1]:.2f}")
    usable = [c for c in d["coupons"] if c["min"] <= subtotal and (coupons or c["how"] in ("automatic", "collected"))]
    cp = max(usable, default=None, key=lambda c: c["off"])
    if cp:
        save += cp["off"]
        parts.append(f"coupon {cp['off']:.2f} ({cp['how']})")
    return save, " + ".join(parts)


def coupon_to_collect(d: Optional[dict], subtotal: float) -> Optional[dict]:
    """
    The plain "Collect" coupon worth collecting for this store subtotal: it
    applies, and it beats the best coupon already usable (one per order).
    """
    if not d:
        return None
    have = max((c["off"] for c in d["coupons"]
                if c["min"] <= subtotal and c["how"] in ("automatic", "collected")), default=0.0)
    best = max((c for c in d["coupons"] if c["how"] == "collect" and c["min"] <= subtotal and c["off"] > have),
               default=None, key=lambda c: c["off"])
    return best


def next_discount(d: Optional[dict], subtotal: float, currency: str, reach: float = 0.5) -> Optional[str]:
    """'add 3.20 → 1.82 store discount' when the next tier/coupon is within `reach`·subtotal."""
    if not d:
        return None
    now, _ = discount_for(d, subtotal)
    best = None
    for need in sorted({t[0] for t in d["tiers"]} | {c["min"] for c in d["coupons"]}):
        if need <= subtotal or need - subtotal > reach * max(subtotal, 1):
            continue
        gain = discount_for(d, need)[0] - now
        if gain > 0.01 and (not best or gain / (need - subtotal) > best[1] / (best[0] - subtotal)):
            best = (need, gain)
    if not best:
        return None
    return f"spend {best[0] - subtotal:.2f} {currency} more in this store → {best[1]:.2f} more off"


def fmt_discounts(d: Optional[dict], currency: str) -> list[str]:
    if not d:
        return []
    out = []
    if d["tiers"]:
        out.append("store discount (automatic): " + ", ".join(f"{o:.2f} off over {m:.2f}" for m, o in d["tiers"]))
    for c in d["coupons"]:
        code = f", code {c['code']}" if c.get("code") and c["how"] != "collected" else ""
        out.append(f"coupon: {c['off']:.2f} off over {c['min']:.2f} — {c['label']}, {c['how']}{code}")
    if d["coins_pct"]:
        have = f", you have {d['coins']} coins" if d["coins"] is not None else ""
        out.append(f"coins: {d['coins_pct']:g}% off{have}")
    out += [f"other: {t}" for t in d["other"]]
    if not out:
        return []
    return [f"discounts ({currency}; one coupon per store and order):"] + [f"  {x}" for x in out]
