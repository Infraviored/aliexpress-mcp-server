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
   the shipping-panel API at qty 1 and 2.
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
) -> list[dict]:
    """
    All ways to get >= `need` pieces from one ladder, using up to two pack
    sizes, ranked by total (goods + shipping). Each way:
    {"lines": [(variant, qty)], "pieces", "goods", "ship", "total"}.
    `choice_free`: shipping counts as 0 (Choice item in an order above threshold).
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
                units = qa + qb
                ship = 0.0 if choice_free else shipping_for(quote, units)
                ways.append({
                    "lines": [(v, q) for v, q in ((a, qa), (b, qb)) if q],
                    "pieces": pieces, "goods": goods, "ship": ship, "total": goods + ship,
                })
    ways.sort(key=lambda w: (w["total"], -w["pieces"]))
    return ways[:limit]


def fmt_way(w: dict, currency: str) -> str:
    parts = " + ".join(f"{q}× {v['label']}" for v, q in w["lines"])
    ship = f" + {w['ship']:.2f} ship" if w["ship"] else ""
    return f"{parts} = {w['pieces']} pcs, {w['goods']:.2f}{ship} = {w['total']:.2f} {currency} ({w['total'] / w['pieces']:.2f}/pc)"


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
