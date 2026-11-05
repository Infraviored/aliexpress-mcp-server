"""Reading "My orders": parsing of mtop.aliexpress.trade.buyer.order.list / .detail.

Pure functions, no network. The list is an Ultron page (blocks with `fields`);
later pages are requested by sending the state of the previous answer back.
Read only: nothing here pays, confirms or changes an order.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

LIST_API, DETAIL_API = "mtop.aliexpress.trade.buyer.order.list", "mtop.aliexpress.trade.buyer.order.detail"

# Normalised states (what the callers filter on) and the tab AliExpress itself has for them.
STATES = ["wartet_auf_zahlung", "in_bearbeitung", "versandt", "zugestellt", "abgeschlossen",
          "storniert", "rueckerstattung", "unbekannt"]
TAB_FOR = {"wartet_auf_zahlung": "unpaid", "in_bearbeitung": "processing",
           "versandt": "shipped", "abgeschlossen": "completed"}
# Keyword -> state, first hit wins (German and English page texts). Order matters:
# "Rückerstattung" must beat "abgeschlossen" in "Rückerstattung abgeschlossen".
_WORDS = [
    ("rueckerstattung", ("rückerstattung", "erstattet", "refund", "dispute", "streit")),
    ("storniert", ("storniert", "geschlossen", "cancel", "closed")),
    ("wartet_auf_zahlung", ("zu zahlen", "zahlung ausstehend", "awaiting payment", "unpaid", "pending payment")),
    ("zugestellt", ("zugestellt", "geliefert", "delivered", "bestätigung", "awaiting confirmation")),
    ("abgeschlossen", ("abgeschlossen", "completed", "finished")),
    ("versandt", ("versandt", "verschickt", "unterwegs", "shipped", "in transit", "verarbeitet")),
    ("in_bearbeitung", ("verarbeitet", "bearbeitung", "processing", "vorbereitung", "awaiting shipment")),
]
_MONTHS = {m: i + 1 for i, names in enumerate([
    ("jan", "januar", "january"), ("feb", "februar", "february"), ("mär", "mrz", "mar", "märz", "march"),
    ("apr", "april"), ("mai", "may"), ("jun", "juni", "june"), ("jul", "juli", "july"),
    ("aug", "august"), ("sep", "sept", "september"), ("okt", "oct", "oktober", "october"),
    ("nov", "november"), ("dez", "dec", "dezember", "december")]) for m in names}


def status_of(text: str, code: Any = None) -> str:
    """Normalised state from the page's status text; the numeric code 9 is "completed"."""
    low = (text or "").lower()
    for state, words in _WORDS:
        # Word start only: "geschlossen" (closed) must not match "abgeschlossen" (completed).
        if any(re.search(r"\b" + re.escape(w), low) for w in words):
            # "Verarbeitet" (shipped tab) must not hide "Wird verarbeitet" (processing).
            if state == "versandt" and "wird verarbeitet" in low:
                continue
            return state
    return "abgeschlossen" if str(code) == "9" else "unbekannt"


def iso_date(text: str) -> str:
    """'21. Okt 2026' / 'Oct 21, 2026' -> '2026-10-21' ('' when unreadable)."""
    iso = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", text or "")
    if iso:
        return "-".join(iso.groups())
    m = re.search(r"(\d{1,2})\.?\s+([A-Za-zäÄ]+)\.?,?\s+(\d{4})", text or "")
    if m:
        day, mon, year = m.groups()
    else:
        m = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", text or "")
        if not m:
            return ""
        mon, day, year = m.groups()
    month = _MONTHS.get(mon.lower())
    return f"{int(year):04d}-{month:02d}-{int(day):02d}" if month else ""


def price(info: str) -> Optional[float]:
    """'13,53€|13|53' -> 13.53."""
    parts = (info or "").split("|")
    try:
        return float(f"{int(parts[1])}.{parts[2]}") if len(parts) >= 3 else None
    except ValueError:
        return None


def _url(u: str) -> str:
    return "https:" + u if (u or "").startswith("//") else (u or "")


def _blocks(resp: dict) -> dict:
    return ((resp.get("data") or {}).get("data")) or {}


def parse_list(resp: dict) -> tuple[list[dict], dict]:
    """Orders of one answer plus the state needed to ask for the next page."""
    blocks = _blocks(resp)
    orders = []
    for key, blk in blocks.items():
        if not key.startswith("pc_om_list_order_"):
            continue
        f = blk.get("fields") or {}
        code = ((f.get("utParams") or {}).get("args") or {}).get("orderStatus")
        items = []
        for ln in f.get("orderLines") or []:
            items.append({
                "item_id": str(ln.get("productId") or ""),
                "sku_id": str(ln.get("skuId") or ""),
                "title": ln.get("itemTitle") or "",
                "variant": ", ".join(f"{a.get('name')}: {a.get('text')}" for a in ln.get("skuAttrs") or []),
                "quantity": ln.get("quantity"),
                "unit_price": price(ln.get("formatPriceInfo")),
                "image": _url(ln.get("itemImgUrl")),
                "url": _url(ln.get("itemDetailUrl")),
                "note": re.sub(r"<[^>]+>", "", ln.get("itemExtraInfo") or ""),
            })
        orders.append({
            "order_id": str(f.get("orderId") or ""),
            "date": iso_date(f.get("orderDateText")),
            "date_text": f.get("orderDateText") or "",
            "total": price(f.get("formatPriceInfo")),
            "currency": f.get("currencyCode") or "",
            "status": status_of(f.get("statusText"), code),
            "status_text": f.get("statusText") or "",
            "status_code": code,
            "store": f.get("storeName") or "",
            "store_url": _url(f.get("storePageUrl")),
            "url": _url(f.get("orderDetailUrl")),
            "items": items,
        })
    body_key = next((k for k in blocks if k.startswith("pc_om_list_body_")), "")
    head_key = next((k for k in blocks if k.startswith("pc_om_list_header_action_")), "")
    body = (blocks.get(body_key) or {}).get("fields") or {}
    d = resp.get("data") or {}
    state = {"has_more": bool(body.get("hasMore")), "page": int(body.get("pageIndex") or 1),
             "body_key": body_key, "head_key": head_key, "blocks": blocks,
             "linkage": d.get("linkage"), "hierarchy": d.get("hierarchy"), "endpoint": d.get("endpoint")}
    return orders, state


def init_payload(tab: str = "all") -> dict:
    return {"statusTab": None if tab in ("", "all") else tab, "renderType": "init", "clientPlatform": "pc",
            "shipToCountry": "DE", "_lang": "de_DE", "timeZone": "GMT+0100"}


def next_payload(state: dict, tab: str = "all") -> dict:
    """The request of the page after `state`: the previous answer's state with the page index raised."""
    blocks, bk, hk = state["blocks"], state["body_key"], state["head_key"]
    body = json.loads(json.dumps(blocks[bk]))
    body["fields"]["pageIndex"] = state["page"] + 1
    data = {bk: body}
    if hk:
        head = json.loads(json.dumps(blocks[hk]))
        head["fields"]["statusTab"] = tab or "all"
        data[hk] = head
    linkage = {k: v for k, v in (state["linkage"] or {}).items() if k in ("common", "input", "request", "signature")}
    params = {"data": json.dumps(data, ensure_ascii=False), "linkage": json.dumps(linkage, ensure_ascii=False),
              "hierarchy": json.dumps({"structure": (state["hierarchy"] or {}).get("structure")}, ensure_ascii=False),
              "endpoint": json.dumps(state["endpoint"], ensure_ascii=False), "operator": bk}
    return {"params": json.dumps(params, ensure_ascii=False), "shipToCountry": "DE", "_lang": "de_DE"}


def detail_payload(order_id: str) -> dict:
    return {"tradeOrderId": str(order_id), "clientPlatform": "pc", "shipToCountry": "DE",
            "_lang": "de_DE", "timeZone": "GMT+0100"}


_TRACK_KEYS = re.compile(r"(tracking|mailno|logisticsno|trackno)", re.I)
_NOT_TRACK = re.compile(r"(status|title|text|url|desc|name|time|date)", re.I)


def _walk(o: Any):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def parse_detail(resp: dict) -> dict:
    """Delivery date, tracking number and the dated steps of one order. No address, no phone."""
    out: dict = {"eta": "", "eta_iso": "", "tracking": [], "paid": "", "shipped": "", "finished": "",
                 "shipping": "", "import_charges": ""}
    for key, blk in _blocks(resp).items():
        f = blk.get("fields") or {}
        if key.startswith("detail_simple_order_info"):
            out["paid"], out["shipped"] = iso_date(f.get("payTime")), iso_date(f.get("orderShipTime"))
            out["finished"] = iso_date(f.get("orderEndTime"))
        elif key.startswith("detail_order_price_block"):
            for row in f.get("priceDetails") or []:
                t = (row.get("title") or "").lower()
                if t.startswith("versand") or t.startswith("shipping"):
                    out["shipping"] = row.get("value") or ""
                elif "einfuhr" in t or "import" in t:
                    out["import_charges"] = row.get("value") or ""
        elif key.startswith("detail_product_block"):
            for p in f.get("productVOList") or []:
                title = ((p.get("etaAndTrack") or {}).get("expectDeliveryDateTitle")) or ""
                if title and not out["eta"]:
                    out["eta"] = title.split(":", 1)[-1].strip()
                    out["eta_iso"] = iso_date(out["eta"])
        for k, v in _walk(f):
            if (_TRACK_KEYS.search(str(k)) and not _NOT_TRACK.search(str(k)) and isinstance(v, str)
                    and re.fullmatch(r"[A-Za-z0-9]{8,32}", v) and any(c.isdigit() for c in v)):
                if v not in out["tracking"]:
                    out["tracking"].append(v)
    return out
