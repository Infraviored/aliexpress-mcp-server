#!/usr/bin/env python3
"""
AliExpress MCP Server

Search AliExpress, pull clean product details, check shipping,
list the whole cart and remove items from it.

Auth: Session cookies from MCP Auth Bridge extension at
~/.mcp-credentials/aliexpress.json
"""

import base64
import gzip
import json
import os
import re
import time
import hashlib
import logging
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus, quote

import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP, Image

import deals

# ─── Configuration ──────────────────────────────────────────────────────────

CREDENTIALS_PATH = Path(
    os.environ.get("ALIEXPRESS_CREDENTIALS", "~/.mcp-credentials/aliexpress.json")
).expanduser()

COUNTRY = os.environ.get("ALIEXPRESS_COUNTRY", "DE")
CURRENCY = os.environ.get("ALIEXPRESS_CURRENCY", "EUR")
ACCEPT_LANGUAGE = os.environ.get("ALIEXPRESS_ACCEPT_LANGUAGE", "de-DE,de;q=0.9,en;q=0.8")

BASE_URL = "https://www.aliexpress.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aliexpress-mcp")


# ─── Auth ───────────────────────────────────────────────────────────────────

def load_cookies() -> dict[str, str]:
    """Load session cookies from the credential file written by MCP Auth Bridge."""
    if not CREDENTIALS_PATH.exists():
        return {}
    try:
        data = json.loads(CREDENTIALS_PATH.read_text())
        return data.get("cookies", {})
    except (json.JSONDecodeError, KeyError):
        return {}


def get_client(require_auth: bool = False, referer: str = BASE_URL) -> httpx.Client:
    """Create an HTTP client with session cookies and realistic browser headers."""
    cookies = load_cookies()

    if require_auth and not cookies:
        raise ValueError(
            "No AliExpress session found. Open aliexpress.com in Chrome, "
            "log in, and click 'Save AliExpress' in the MCP Auth Bridge extension."
        )

    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items()) if cookies else ""

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": ACCEPT_LANGUAGE,
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": referer,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }
    if cookie_str:
        headers["Cookie"] = cookie_str

    return httpx.Client(
        base_url=BASE_URL,
        headers=headers,
        follow_redirects=True,
        timeout=30.0,
    )


def check_auth_redirect(response: httpx.Response) -> bool:
    """Detect a redirect to the AliExpress login page (expired session)."""
    url = str(response.url).lower()
    return "login.aliexpress" in url or "/login.htm" in url or "passport" in url


AUTH_EXPIRED_MSG = (
    "AliExpress session expired or missing. Open aliexpress.com in Chrome, "
    "log in, and click 'Save AliExpress' in the MCP Auth Bridge extension."
)


# ─── MTOP API Client ────────────────────────────────────────────────────────
#
# The AliExpress product detail page is client-side-rendered — window.runParams
# is empty in the initial HTML. Real data loads via signed MTOP AJAX calls.
# We replicate the signing algorithm the `mtop.js` library uses in-browser:
#
#   token   = cookie `_m_h5_tk` split on `_` → first segment
#   appKey  = "12574478" (the public web app key)
#   t       = current timestamp in ms as a string
#   data    = JSON string of the request payload
#   sign    = md5( token + "&" + t + "&" + appKey + "&" + data )
#
# The response is wrapped in an HTTP envelope. When ret[0] starts with
# "FAIL_SYS_TOKEN_EMPTY" or "FAIL_SYS_TOKEN_EXPIRED" (sometimes spelled EXOIRED) the server returns fresh
# `_m_h5_tk` cookies — the real browser retries; we can too.

MTOP_APP_KEY = "12574478"
MTOP_BASE = "https://acs.aliexpress.com"


def _mtop_sign(token: str, t_ms: str, app_key: str, data: str) -> str:
    raw = f"{token}&{t_ms}&{app_key}&{data}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _h5_token_prefix(cookies: dict[str, str]) -> Optional[str]:
    """Extract the signing token prefix from the _m_h5_tk cookie."""
    raw = cookies.get("_m_h5_tk", "")
    if not raw:
        return None
    # Cookie shape: <token>_<expiry-ms>
    return raw.split("_", 1)[0] or None


def mtop_call(
    api: str,
    version: str,
    payload: dict[str, Any],
    *,
    cookies: Optional[dict[str, str]] = None,
    retries: int = 1,
    referer: Optional[str] = None,
    method: str = "GET",
) -> dict:
    """
    Make a signed MTOP request (`method` POST sends `data` as a form body). Returns the parsed JSON response dict on success.

    Raises RuntimeError on signing/network failure.
    """
    if cookies is None:
        cookies = load_cookies()
    _mtop_pace()

    token = _h5_token_prefix(cookies)
    if not token:
        raise RuntimeError(
            "No _m_h5_tk cookie found. Re-save AliExpress credentials via the "
            "MCP Auth Bridge extension."
        )

    data_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    t_ms = str(int(time.time() * 1000))
    sign = _mtop_sign(token, t_ms, MTOP_APP_KEY, data_str)

    params = {
        "jsv": "2.6.2",
        "appKey": MTOP_APP_KEY,
        "t": t_ms,
        "sign": sign,
        "api": api,
        "v": version,
        "type": "originaljson",
        "dataType": "json",
        "timeout": "20000",
        "AntiCreep": "true",
        "AntiFlood": "true",
        "data": data_str,
    }

    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": ACCEPT_LANGUAGE,
        "Referer": referer or f"{BASE_URL}/",
        "Origin": BASE_URL,
        "Cookie": cookie_str,
    }
    url = f"{MTOP_BASE}/h5/{api}/{version}/"

    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        if method.upper() == "POST":
            body = {"data": params.pop("data")}
            params["type"] = "originaljson"
            resp = c.post(url, params=params, data=body, headers=headers)
        else:
            resp = c.get(url, params=params, headers=headers)

        # MTOP sometimes wraps valid JSON inside `mtopjsonp1({...})` even when
        # we request originaljson. Strip the wrapper if present.
        text = resp.text.strip()
        if text.startswith("mtopjsonp") and text.endswith(")"):
            inner = text.split("(", 1)[1][:-1]
            text = inner

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"MTOP returned non-JSON: {text[:200]}") from e

        ret = data.get("ret", [])
        ret_str = ret[0] if ret else ""

        # Retry once if the token expired (the server will have set fresh cookies).
        # httpx.Response.cookies iterates as cookie-name strings, so pull values by key.
        if retries > 0 and any(
            s in ret_str for s in ("FAIL_SYS_TOKEN_EMPTY", "FAIL_SYS_TOKEN_EXPIRED", "FAIL_SYS_TOKEN_EXOIRED", "TOKEN_EMPTY", "TOKEN_EXPIRED")
        ):
            new_cookies = dict(cookies)
            for name in resp.cookies:
                val = resp.cookies.get(name)
                if val is not None:
                    new_cookies[name] = val
            return mtop_call(api, version, payload, cookies=new_cookies, retries=retries - 1, referer=referer, method=method)

        # Bot check: solve the slider (or click the reCAPTCHA box) in real Chrome
        # on a virtual display, retry once.
        punish_url = (data.get("data") or {}).get("url") if isinstance(data.get("data"), dict) else None
        if "FAIL_SYS_USER_VALIDATE" in ret_str:
            BOT_CHECK["since"] = BOT_CHECK.get("since") or time.time()
        # `action=deny` (after hammering past a solved slider) has no slider: wait it out.
        if retries > 0 and punish_url and "action=captcha" in punish_url:
            import slider
            logger.info("MTOP bot check on %s, solving slider", api)
            try:
                solved = slider.solve(punish_url, CREDENTIALS_PATH)
            except Exception as e:
                logger.warning("slider solver failed: %s", e)
                solved = False
            if solved:
                BOT_CHECK.clear()
                return mtop_call(api, version, payload, cookies=load_cookies(), retries=retries - 1, referer=referer, method=method)
        elif ret_str.startswith("SUCCESS"):
            BOT_CHECK.clear()

        return data


# Set while AliExpress answers with a bot check we could not pass.
BOT_CHECK: dict[str, float] = {}
MTOP_MIN_GAP = float(os.environ.get("ALIEXPRESS_MTOP_GAP", "0.8"))
_last_mtop = [0.0]


def _mtop_pace() -> None:
    """Keep a small gap between MTOP calls: bursts are what trigger the bot check."""
    wait = _last_mtop[0] + MTOP_MIN_GAP - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_mtop[0] = time.time()


def blocked_msg() -> str:
    """Why a lookup came back empty, when it was the bot check."""
    if not BOT_CHECK:
        return ""
    mins = int((time.time() - BOT_CHECK["since"]) / 60)
    return (f" AliExpress bot check (captcha) not passed, blocked for {mins} min."
            " Wait 10-30 min, or pass it once in a browser and re-import cookies.")


def _walk_find(obj: Any, match_keys: set) -> Any:
    """Depth-first walk; return the first value whose key is in match_keys."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in match_keys and v not in (None, "", {}, []):
                return v
        for v in obj.values():
            r = _walk_find(v, match_keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _walk_find(v, match_keys)
            if r is not None:
                return r
    return None


# ─── Parsing Helpers ────────────────────────────────────────────────────────

PRICE_RE = re.compile(
    r"(?:C\$|CA\$|US\$|\$|€|CAD|USD|EUR)\s*"
    r"([\d.,]+)"
    r"|([\d.,]+)\s*(?:€|EUR)",
)
ITEM_ID_RE = re.compile(r"/item/(\d+)\.html")


def _norm_price(raw: str) -> Optional[float]:
    """Normalise a captured price string to a float.

    Handles both '1,234.56' (en) and '1.234,56' / '1234,56' (de/fr) by
    checking whether the *last* separator is a comma (European) or a dot.
    """
    raw = raw.strip()
    if not raw:
        return None
    # European: comma is the decimal separator, dots are thousands
    if "," in raw and ("." not in raw or raw.rfind(",") > raw.rfind(".")):
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_price(text: str) -> Optional[float]:
    """Pull the first price-looking number out of a string."""
    if not text:
        return None
    m = PRICE_RE.search(text)
    if m:
        return _norm_price(m.group(1) or m.group(2))
    # Fallback: bare number (dot-decimal)
    m = re.search(r"([\d,]+\.\d{2})", text)
    if m:
        return _norm_price(m.group(1))
    return None


def _extract_embedded_json(html: str, var_names: list[str]) -> Optional[dict]:
    """
    AliExpress embeds product/search data in <script> tags like:
        window.runParams = {...}
        window._d_c_._hycdylydh = function() { ... data: {...} }
    Try to grab the JSON blob.
    """
    for name in var_names:
        pattern = re.compile(
            r"window\." + re.escape(name) + r"\s*=\s*(\{.*?\});?\s*\n",
            re.DOTALL,
        )
        m = pattern.search(html)
        if m:
            blob = m.group(1)
            try:
                return json.loads(blob)
            except json.JSONDecodeError:
                pass

    # Generic: look for `data: {...}` inside the runParams pattern
    m = re.search(r"window\.runParams\s*=\s*(\{.*?\});", html, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return None
    return None


def _parse_search_cards(html: str) -> list[dict]:
    """
    Parse the server-rendered result cards (`a.search-card-item`).

    The card link carries the hard numbers:
      pdp_npi   = "6@dis!EUR!<original>!<sale>!..."  (prices of the default SKU)
      pdp_ext_f = {"order": "<orders>", ...}
    and the card text the rest: "+10 Tsd. verkauft | 4.9 | ... | Kostenloser Versand ab 10,00€".
    """
    from urllib.parse import urlparse, parse_qs

    soup = BeautifulSoup(html, "html.parser")
    products: list[dict] = []
    seen: set[str] = set()
    for card in soup.select("a.search-card-item"):
        href = card.get("href", "")
        m = ITEM_ID_RE.search(href)
        if not m or m.group(1) in seen:
            continue
        pid = m.group(1)
        q = parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query)

        sale = original = None
        currency = None
        npi = (q.get("pdp_npi") or [""])[0].split("!")
        if len(npi) >= 4:
            currency = npi[1] or None
            try:
                original = float(npi[2]) if npi[2] else None
                sale = float(npi[3]) if npi[3] else None
            except ValueError:
                pass

        orders = None
        try:
            orders = int(json.loads((q.get("pdp_ext_f") or ["{}"])[0]).get("order"))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass

        title_el = card.select_one("[title]")
        title = (title_el.get("title") if title_el else None) or card.get_text(" ", strip=True)[:120]

        parts = [t.strip() for t in card.get_text("|", strip=True).split("|") if t.strip()]
        rating = None
        for t in parts:
            if re.fullmatch(r"[0-5]\.\d", t):
                rating = float(t)
                break
        sold = next((t for t in parts if re.search(r"verkauft|sold", t, re.I)), None)
        shipping = next((t for t in parts if re.search(r"versand|shipping|delivery", t, re.I)), None)
        choice = bool(card.select_one("img[src*='S135d2af8d57840b28092ceba069bdb6c4']")) or "Choice" in parts

        seen.add(pid)
        products.append({
            "item_id": pid,
            "title": title.strip()[:200],
            "price": sale,
            "original_price": original if original and sale and original > sale else None,
            "currency": currency,
            "discount_pct": round(100 * (1 - sale / original)) if sale and original and original > sale else None,
            "rating": rating,
            "orders": orders,
            "sold_count": sold,
            "shipping": shipping,
            "choice": choice,
            "url": f"{BASE_URL}/item/{pid}.html",
        })
    return products


def parse_search_results(html: str) -> list[dict]:
    """Parse product cards from an AliExpress search results page."""
    cards = _parse_search_cards(html)
    if cards:
        return cards

    products: list[dict] = []
    seen_ids: set[str] = set()

    # Approach 1: extract from embedded JSON (most reliable)
    data = _extract_embedded_json(html, ["runParams", "_dida_"])
    if data:
        # Walk the structure looking for lists of items
        items_pool: list[dict] = []

        def walk(obj):
            if isinstance(obj, dict):
                # Common keys where items live
                for k in ("mods", "items", "itemList", "resultList"):
                    v = obj.get(k)
                    if isinstance(v, list):
                        for it in v:
                            if isinstance(it, dict):
                                items_pool.append(it)
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        walk(data)

        for it in items_pool:
            # Heuristic: needs a productId or itemId
            pid = (
                it.get("productId")
                or it.get("itemId")
                or it.get("product_id")
                or it.get("id")
            )
            title = (
                it.get("title", {}).get("displayTitle")
                if isinstance(it.get("title"), dict)
                else it.get("title")
            ) or it.get("subject") or it.get("name")
            if not pid or not title:
                continue
            pid = str(pid)
            if pid in seen_ids:
                continue

            price_info = it.get("prices") or it.get("price") or {}
            sale_price = None
            original_price = None
            if isinstance(price_info, dict):
                sp = price_info.get("salePrice") or price_info.get("formattedPrice")
                op = price_info.get("originalPrice") or price_info.get("strikePrice")
                if isinstance(sp, dict):
                    sale_price = sp.get("minPrice") or parse_price(sp.get("formattedPrice", ""))
                elif sp:
                    sale_price = parse_price(str(sp))
                if isinstance(op, dict):
                    original_price = op.get("minPrice") or parse_price(op.get("formattedPrice", ""))
                elif op:
                    original_price = parse_price(str(op))

            discount_pct = None
            d = it.get("discount") or (price_info.get("discount") if isinstance(price_info, dict) else None)
            if d:
                dm = re.search(r"(\d+)", str(d))
                if dm:
                    discount_pct = int(dm.group(1))

            trade = it.get("trade") or {}
            sold_count = trade.get("tradeDesc") if isinstance(trade, dict) else None

            rating = None
            ev = it.get("evaluation") or {}
            if isinstance(ev, dict):
                r = ev.get("starRating") or ev.get("rating")
                if r:
                    try:
                        rating = float(r)
                    except (TypeError, ValueError):
                        rating = None

            seen_ids.add(pid)
            products.append({
                "item_id": pid,
                "title": title.strip(),
                "price": sale_price,
                "original_price": original_price,
                "discount_pct": discount_pct,
                "rating": rating,
                "sold_count": sold_count,
                "url": f"{BASE_URL}/item/{pid}.html",
            })

        if products:
            return products

    # Approach 2: HTML fallback
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.select("a[href*='/item/']"):
        href = link.get("href", "")
        m = ITEM_ID_RE.search(href)
        if not m:
            continue
        pid = m.group(1)
        if pid in seen_ids:
            continue

        # Climb up to the card container
        card = link.find_parent(["div", "article", "li"]) or link
        title_el = card.select_one("h1, h2, h3, [class*='title'], [class*='Title']")
        title = title_el.get_text(strip=True) if title_el else link.get("title") or ""
        if not title:
            continue

        card_text = card.get_text(" ", strip=True)
        sale_price = parse_price(card_text)

        # Try to find a second (higher) price for original
        original_price = None
        all_prices = [parse_price(t) for t in re.findall(r"[A-Z]{0,2}\$?\s*[\d,]+\.\d{2}", card_text)]
        all_prices = [p for p in all_prices if p is not None]
        if len(all_prices) >= 2:
            sale_price = min(all_prices)
            original_price = max(all_prices)

        discount_pct = None
        dm = re.search(r"-?(\d{1,2})%", card_text)
        if dm:
            discount_pct = int(dm.group(1))

        sold_match = re.search(r"([\d,]+\+?)\s*sold", card_text, re.IGNORECASE)
        sold_count = sold_match.group(0) if sold_match else None

        rating = None
        rmatch = re.search(r"\b([0-4]\.\d|5\.0)\b", card_text)
        if rmatch:
            try:
                rating = float(rmatch.group(1))
            except ValueError:
                pass

        seen_ids.add(pid)
        products.append({
            "item_id": pid,
            "title": title[:200],
            "price": sale_price,
            "original_price": original_price,
            "discount_pct": discount_pct,
            "rating": rating,
            "sold_count": sold_count,
            "url": f"{BASE_URL}/item/{pid}.html",
        })

    return products


def parse_product_detail(html: str, item_id: str) -> dict:
    """Parse a product detail page."""
    soup = BeautifulSoup(html, "html.parser")

    details = {
        "item_id": item_id,
        "url": f"{BASE_URL}/item/{item_id}.html",
        "title": None,
        "price": None,
        "original_price": None,
        "discount_pct": None,
        "rating": None,
        "sold_count": None,
        "seller_name": None,
        "shipping_cost": None,
        "shipping_estimate": None,
        "variants": [],
    }

    # Title
    if soup.title:
        t = soup.title.get_text(strip=True)
        # AliExpress titles often have " | AliExpress" or similar suffix
        t = re.sub(r"\s*[|–-]\s*(AliExpress|aliexpress).*$", "", t, flags=re.IGNORECASE)
        details["title"] = t or None

    h1 = soup.select_one("h1")
    if h1 and h1.get_text(strip=True):
        details["title"] = h1.get_text(strip=True)

    # Try embedded JSON first
    data = _extract_embedded_json(html, ["runParams"])
    if data:
        # Walk for useful nuggets
        def find_first(obj, keys):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in keys and v not in (None, "", {}):
                        return v
                    r = find_first(v, keys)
                    if r is not None:
                        return r
            elif isinstance(obj, list):
                for v in obj:
                    r = find_first(v, keys)
                    if r is not None:
                        return r
            return None

        price_node = find_first(data, {"priceModule", "price", "priceInfo"})
        if isinstance(price_node, dict):
            sp = price_node.get("minActivityAmount") or price_node.get("formatedActivityPrice") or price_node.get("formatedPrice")
            op = price_node.get("formatedPrewarmingPrice") or price_node.get("maxAmount")
            if isinstance(sp, dict):
                details["price"] = sp.get("value") or parse_price(sp.get("formattedPrice", ""))
            elif sp:
                details["price"] = parse_price(str(sp))
            if isinstance(op, dict):
                details["original_price"] = op.get("value")
            elif op:
                details["original_price"] = parse_price(str(op))

        title_node = find_first(data, {"title", "subject"})
        if isinstance(title_node, str) and not details["title"]:
            details["title"] = title_node

        seller_node = find_first(data, {"storeName", "sellerName", "storeInfo"})
        if isinstance(seller_node, str):
            details["seller_name"] = seller_node
        elif isinstance(seller_node, dict):
            details["seller_name"] = seller_node.get("name") or seller_node.get("storeName")

        ship_node = find_first(data, {"shippingModule", "logistics"})
        if isinstance(ship_node, dict):
            details["shipping_cost"] = parse_price(str(ship_node.get("formattedFreight", "")))
            details["shipping_estimate"] = ship_node.get("deliveryDate") or ship_node.get("etd")

    # Fallbacks from rendered HTML body text
    body_text = soup.get_text(" ", strip=True)

    if details["price"] is None:
        # Look for a currency-prefixed price near the top
        price_el = soup.select_one("[class*='price'], [class*='Price']")
        if price_el:
            details["price"] = parse_price(price_el.get_text(" ", strip=True))

    # Discount
    dmatch = re.search(r"-(\d{1,2})%", body_text)
    if dmatch:
        details["discount_pct"] = int(dmatch.group(1))

    # Rating
    rmatch = re.search(r"\b([0-4]\.\d|5\.0)\s*(?:out of|/)\s*5", body_text)
    if rmatch:
        try:
            details["rating"] = float(rmatch.group(1))
        except ValueError:
            pass
    elif details["rating"] is None:
        rmatch = re.search(r"\b([0-4]\.\d|5\.0)\b\s*\(?\d", body_text)
        if rmatch:
            try:
                details["rating"] = float(rmatch.group(1))
            except ValueError:
                pass

    # Sold count
    sold_match = re.search(r"([\d,]+\+?)\s*sold", body_text, re.IGNORECASE)
    if sold_match:
        details["sold_count"] = sold_match.group(0)

    return details


def parse_cart(html: str) -> dict:
    """Parse cart page HTML for item list and totals."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)

    result: dict = {
        "items": [],
        "subtotal": None,
        "shipping_total": None,
        "grand_total": None,
        "raw_snippet": None,
    }

    # Extract item links
    items_by_id: dict[str, dict] = {}
    for link in soup.select("a[href*='/item/']"):
        href = link.get("href", "")
        m = ITEM_ID_RE.search(href)
        if not m:
            continue
        pid = m.group(1)
        if pid in items_by_id:
            continue
        title = link.get("title") or link.get_text(strip=True)
        if not title or len(title) < 4:
            continue

        card = link.find_parent(["div", "li", "article"]) or link
        card_text = card.get_text(" ", strip=True)
        all_prices = [parse_price(t) for t in re.findall(r"[A-Z]{0,2}\$\s*[\d,]+\.\d{2}", card_text)]
        all_prices = [p for p in all_prices if p is not None]
        sale_price = min(all_prices) if all_prices else None

        qty_match = re.search(r"(?:Qty|Quantity|x)\s*[:\s]\s*(\d+)", card_text, re.IGNORECASE)
        qty = int(qty_match.group(1)) if qty_match else 1

        items_by_id[pid] = {
            "item_id": pid,
            "title": title[:200],
            "price": sale_price,
            "quantity": qty,
            "url": f"{BASE_URL}/item/{pid}.html",
        }

    result["items"] = list(items_by_id.values())

    # Totals — search body text
    for label, key in [
        (r"Subtotal", "subtotal"),
        (r"Shipping", "shipping_total"),
        (r"(?:Grand\s*)?Total", "grand_total"),
    ]:
        m = re.search(label + r"[^\n$]*?([A-Z]{0,2}\$\s*[\d,]+\.\d{2})", text, re.IGNORECASE)
        if m:
            result[key] = parse_price(m.group(1))

    result["raw_snippet"] = text[:500]
    return result


# ─── MCP Server ─────────────────────────────────────────────────────────────

mcp = FastMCP("aliexpress", dependencies=["httpx", "beautifulsoup4"])


@mcp.tool()
def search_products(
    query: str,
    min_rating: float = 0,
    max_price: float = 0,
    sort_by: str = "best_match",
    page: int = 1,
    with_shipping: bool = False,
) -> str:
    """
    Search AliExpress for products (12 results per page).

    The price shown is the CHEAPEST variant, which is often bait: an
    accessory, a bare chip or 1 piece of a pack (ESP32-S3 "6.19 €" was a
    breakout board; the dev board was 12.19 €). Judge with best_offer.
    Each hit also shows orders, rating and the
    shipping line ("Kostenloser Versand ab 10,00€" = free above that cart value).
    Pack sizes (1/5/10 pcs) are variants with their own prices:
    use get_product_variants before comparing deals.

    Args:
        query: Search term (e.g., "groudon plush", "usb c cable")
        min_rating: Minimum rating (0-5, e.g., 4.5). 0 disables filter.
        max_price: Maximum price in the account currency. 0 disables filter.
        sort_by: One of "best_match", "orders", "price_asc", "price_desc"
        page: Result page, starting at 1.
        with_shipping: Also fetch real shipping to the configured country for the
            first 8 non-Choice hits (one product-page call each, slower).
    """
    # AliExpress URL slug pattern
    slug = quote_plus(query.strip()).replace("+", "-")
    url_path = f"/w/wholesale-{slug}.html"

    sort_map = {
        "best_match": None,
        "orders": "total_tranpro_desc",
        "price_asc": "price_asc",
        "price_desc": "price_desc",
    }
    params = {}
    if sort_map.get(sort_by):
        params["SortType"] = sort_map[sort_by]
    if page > 1:
        params["page"] = page

    client = get_client()
    try:
        resp = client.get(url_path, params=params)
        if check_auth_redirect(resp):
            return AUTH_EXPIRED_MSG
        resp.raise_for_status()

        products = parse_search_results(resp.text)

        # Apply filters
        if min_rating > 0:
            products = [p for p in products if p.get("rating") and p["rating"] >= min_rating]
        if max_price > 0:
            products = [p for p in products if p.get("price") is not None and p["price"] <= max_price]

        if not products:
            filters = []
            if min_rating > 0:
                filters.append(f"rating ≥ {min_rating}")
            if max_price > 0:
                filters.append(f"price ≤ ${max_price:.2f}")
            suffix = f" with {', '.join(filters)}" if filters else ""
            return f"No products found for '{query}'{suffix}."

        if with_shipping:
            for p in [x for x in products if not x.get("choice")][:8]:
                try:
                    offer = _pdp_offer(p["item_id"])
                    if offer:
                        p["shipping"] = _fmt_shipping(offer["shipping"]).replace("shipping to ", "to ")
                except Exception as e:
                    logger.debug("shipping lookup %s failed: %s", p["item_id"], e)
                time.sleep(1.0)

        lines = [f"Found {len(products)} result(s) for '{query}' (sort: {sort_by}, page {page}):"]
        for p in products[:25]:
            line = f"- {p['title']}"
            if p["price"] is not None:
                line += f" — {p['price']:.2f} {p.get('currency') or CURRENCY}"
            if p["original_price"] is not None and p["original_price"] > (p["price"] or 0):
                line += f" (was {p['original_price']:.2f})"
            if p["rating"]:
                line += f" ★{p['rating']}"
            if p.get("orders") is not None:
                line += f" · {p['orders']} orders"
            elif p["sold_count"]:
                line += f" · {p['sold_count']}"
            if p.get("choice"):
                line += " · Choice"
            if p.get("shipping"):
                line += f"\n  shipping: {p['shipping']}"
            line += f"\n  item_id: {p['item_id']}"
            lines.append(line)
        return "\n".join(lines)
    finally:
        client.close()


# Product pages repeat within one deal search (variants, pictures, best_offer).
PDP_TTL = 600
_PDP_CACHE: dict[str, tuple[float, dict]] = {}


def _fetch_pdp_mtop(item_id: str) -> Optional[dict]:
    """
    Call the PDP MTOP endpoint and return the raw response dict, or None on failure.
    Tries the PC endpoint first, then the msite endpoint as a fallback.
    """
    hit = _PDP_CACHE.get(item_id)
    if hit and time.time() - hit[0] < PDP_TTL:
        return hit[1]
    referer = f"{BASE_URL}/item/{item_id}.html"
    payload = {
        "productId": item_id,
        "_currency": CURRENCY,
        # "<lang>_<country>": the country here decides where shipping is quoted to.
        "_lang": f"en_{COUNTRY}",
        "country": COUNTRY,
        "province": "",
        "city": "",
        "channel": "",
        "sourceType": "pc",
        "clientType": "pc",
        "pdp_ext_f": "",
    }

    for api, version in [
        ("mtop.aliexpress.pdp.pc.query", "1.0"),
        ("mtop.aliexpress.itemdetail.pc.asyncPCDetail", "1.0"),
        ("mtop.aliexpress.itemdetail.msite", "1.0"),
    ]:
        try:
            resp = mtop_call(api, version, payload, referer=referer)
        except Exception as e:
            logger.debug("MTOP %s failed: %s", api, e)
            continue
        ret = resp.get("ret", [])
        if ret and any("FAIL_SYS_USER_VALIDATE" in r for r in ret):
            return None
        if ret and any("SUCCESS" in r for r in ret):
            _PDP_CACHE[item_id] = (time.time(), resp)
            return resp
        # Some endpoints return data even without SUCCESS::API_SUCCESS in ret
        if resp.get("data") and isinstance(resp["data"], dict) and resp["data"]:
            return resp
        logger.debug("MTOP %s ret=%s", api, ret)
    return None


def _extract_pdp_fields(mtop_resp: dict, item_id: str) -> dict:
    """
    Pull fields we care about from an MTOP PDP response.

    The response uses a component layout keyed by uppercase section names:
    PRODUCT_TITLE, PRICE, PC_RATING, SHIPPING, SHOP_CARD_PC, HEADER_IMAGE_PC, …
    Field paths below were reverse-engineered from a live response (Apr 2026).
    """
    d: dict[str, Any] = {
        "item_id": item_id,
        "url": f"{BASE_URL}/item/{item_id}.html",
        "title": None,
        "price": None,
        "price_range": None,
        "original_price": None,
        "discount_pct": None,
        "rating": None,
        "review_count": None,
        "sold_count": None,
        "seller_name": None,
        "store_url": None,
        "seller_positive_rate": None,
        "seller_total_reviews": None,
        "shipping_cost": None,
        "shipping_estimate": None,
        "ship_from": None,
        "ship_days_min": None,
        "ship_days_max": None,
        "image_url": None,
    }

    result = mtop_resp.get("data", {}).get("result", {})
    if not isinstance(result, dict):
        return d

    # ── Title ──────────────────────────────────────────────────────────
    pt = result.get("PRODUCT_TITLE")
    if isinstance(pt, dict):
        d["title"] = (
            pt.get("text")          # the real location
            or pt.get("subject")
            or pt.get("title")
            or pt.get("displayTitle")
        )
    if not d["title"]:
        gd = result.get("GLOBAL_DATA", {}).get("globalData", {})
        if isinstance(gd, dict):
            d["title"] = gd.get("subject")

    # ── Price ──────────────────────────────────────────────────────────
    # Real paths (seen live Apr 2026):
    #   PRICE.targetSkuPriceInfo.salePriceString         e.g. "C$9.76"
    #   PRICE.targetSkuPriceInfo.originalPrice.value     e.g. 12.19
    # For variant listings, PRICE.skuPriceInfoMap is a dict of per-SKU prices;
    # we report the min/max range when targetSku isn't enough.
    pn = result.get("PRICE")
    if isinstance(pn, dict):
        tsp = pn.get("targetSkuPriceInfo") or {}
        if isinstance(tsp, dict):
            sp = tsp.get("salePriceString")
            if isinstance(sp, str):
                d["price"] = parse_price(sp)
            op = tsp.get("originalPrice")
            if isinstance(op, dict):
                v = op.get("value")
                if isinstance(v, (int, float)):
                    d["original_price"] = float(v)
                elif op.get("formatedAmount"):
                    d["original_price"] = parse_price(op["formatedAmount"])

        # If this is a variant listing, compute a price range from skuPriceInfoMap
        sku_map = pn.get("skuPriceInfoMap")
        if isinstance(sku_map, dict) and sku_map:
            prices = []
            for sku in sku_map.values():
                if isinstance(sku, dict):
                    sp = sku.get("salePriceString")
                    if isinstance(sp, str):
                        p = parse_price(sp)
                        if p is not None:
                            prices.append(p)
            if prices:
                lo, hi = min(prices), max(prices)
                if lo != hi:
                    d["price_range"] = (lo, hi)
                if d["price"] is None:
                    d["price"] = lo

    if d["discount_pct"] is None and d["price"] and d["original_price"] and d["original_price"] > d["price"]:
        d["discount_pct"] = round((1 - d["price"] / d["original_price"]) * 100)

    # ── Rating / sold count ────────────────────────────────────────────
    rating_mod = result.get("PC_RATING")
    if isinstance(rating_mod, dict):
        r = rating_mod.get("rating")
        if r:
            try:
                d["rating"] = float(r)
            except (TypeError, ValueError):
                pass
        rc = rating_mod.get("totalValidNum")
        if rc is not None:
            try:
                d["review_count"] = int(rc)
            except (TypeError, ValueError):
                pass
        other = rating_mod.get("otherText") or ""
        sm = re.search(r"([\d,]+\+?)\s*sold", other, re.IGNORECASE)
        if sm:
            d["sold_count"] = sm.group(0)

    # ── Seller / store ─────────────────────────────────────────────────
    shop = result.get("SHOP_CARD_PC")
    if isinstance(shop, dict):
        d["seller_name"] = shop.get("storeName")
        si = shop.get("sellerInfo") or {}
        store_url = si.get("storeURL") or shop.get("storeHomePage")
        if isinstance(store_url, str):
            if store_url.startswith("//"):
                store_url = "https:" + store_url
            d["store_url"] = store_url
        pr = shop.get("sellerPositiveRate")
        if pr:
            try:
                d["seller_positive_rate"] = float(pr)
            except (TypeError, ValueError):
                pass
        tn = shop.get("sellerTotalNum")
        if tn is not None:
            try:
                d["seller_total_reviews"] = int(tn)
            except (TypeError, ValueError):
                pass

    # ── Shipping ──────────────────────────────────────────────────────
    ship = result.get("SHIPPING")
    if isinstance(ship, dict):
        # bizData lives inside originalLayoutResultList[0] or deliveryLayoutInfo[0]
        layouts = ship.get("originalLayoutResultList") or ship.get("deliveryLayoutInfo") or []
        biz = None
        if isinstance(layouts, list) and layouts:
            l0 = layouts[0]
            if isinstance(l0, dict):
                biz = l0.get("bizData")
        if isinstance(biz, dict):
            amt = biz.get("displayAmount")
            if amt is not None:
                try:
                    d["shipping_cost"] = float(amt)
                except (TypeError, ValueError):
                    d["shipping_cost"] = parse_price(str(amt))
            if d["shipping_cost"] is None and biz.get("logisticsComposeThreshold"):
                # e.g., "C$0.00" — sometimes the "threshold" is actually the freight text
                d["shipping_cost"] = parse_price(biz["logisticsComposeThreshold"])
            eta_min = biz.get("displayEtaMinDate")
            eta_max = biz.get("displayEtaMaxDate")
            if eta_min and eta_max:
                d["shipping_estimate"] = f"{eta_min} – {eta_max}"
            elif eta_min:
                d["shipping_estimate"] = eta_min
            d["ship_from"] = biz.get("shipFrom")
            dmin = biz.get("deliveryDayMin")
            dmax = biz.get("deliveryDayMax")
            if dmin is not None:
                try:
                    d["ship_days_min"] = int(dmin)
                except (TypeError, ValueError):
                    pass
            if dmax is not None:
                try:
                    d["ship_days_max"] = int(dmax)
                except (TypeError, ValueError):
                    pass

    # ── Image ─────────────────────────────────────────────────────────
    hdr = result.get("HEADER_IMAGE_PC")
    if isinstance(hdr, dict):
        imgs = hdr.get("imagePathList") or hdr.get("imgList")
        if isinstance(imgs, list) and imgs and isinstance(imgs[0], str):
            d["image_url"] = imgs[0]

    return d


@mcp.tool()
def get_product_details(item_id: str = "", url: str = "") -> str:
    """
    Get detailed info for a specific AliExpress product.

    Args:
        item_id: AliExpress item ID (e.g., "1005007655628250")
        url: Full product URL (alternative to item_id)
    """
    if not item_id and url:
        m = ITEM_ID_RE.search(url)
        if m:
            item_id = m.group(1)
    if not item_id:
        return "Provide either item_id or a full AliExpress product url."

    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    # Primary path: MTOP signed API
    d: Optional[dict] = None
    try:
        resp = _fetch_pdp_mtop(item_id)
        if resp:
            d = _extract_pdp_fields(resp, item_id)
    except Exception as e:
        logger.warning("MTOP PDP fetch failed: %s", e)

    # Fallback: HTML scrape (rarely useful since PDP is CSR, but kept for robustness)
    if not d or not (d.get("title") or d.get("price")):
        client = get_client(referer=f"{BASE_URL}/")
        try:
            r = client.get(f"/item/{item_id}.html")
            if check_auth_redirect(r):
                return AUTH_EXPIRED_MSG
            if r.status_code == 404:
                return f"Product not found: item_id {item_id}."
            r.raise_for_status()
            html_d = parse_product_detail(r.text, item_id)
            if not d:
                d = html_d
            else:
                for k, v in html_d.items():
                    if not d.get(k) and v:
                        d[k] = v
        finally:
            client.close()

    if not d or not (d.get("title") or d.get("price")):
        return (
            f"Could not extract product data for item {item_id}. "
            "The MTOP API returned no data — session may be expired or the item "
            "may be region-restricted. Try re-saving credentials."
        )

    lines = [f"# {d.get('title') or item_id}"]
    lines.append(f"URL: {d['url']}")
    if d.get("price") is not None:
        if d.get("price_range"):
            lo, hi = d["price_range"]
            line = f"Price: ${lo:.2f}–${hi:.2f} {CURRENCY}"
        else:
            line = f"Price: ${d['price']:.2f} {CURRENCY}"
        if d.get("original_price") and d["original_price"] > d["price"]:
            line += f" (was ${d['original_price']:.2f}"
            if d.get("discount_pct"):
                line += f", -{d['discount_pct']}%"
            line += ")"
        lines.append(line)
    if d.get("rating"):
        rating_line = f"Rating: ★{d['rating']}"
        if d.get("review_count"):
            rating_line += f" ({d['review_count']} reviews)"
        lines.append(rating_line)
    if d.get("sold_count"):
        lines.append(f"Sold: {d['sold_count']}")
    if d.get("seller_name"):
        seller_line = f"Seller: {d['seller_name']}"
        if d.get("seller_positive_rate"):
            seller_line += f" — {d['seller_positive_rate']}% positive"
        if d.get("seller_total_reviews"):
            seller_line += f" ({d['seller_total_reviews']} reviews)"
        lines.append(seller_line)
        if d.get("store_url"):
            lines.append(f"Store: {d['store_url']}")
    if d.get("shipping_cost") is not None:
        ship_line = "Shipping: " + ("Free" if d["shipping_cost"] == 0 else f"${d['shipping_cost']:.2f} {CURRENCY}")
        lines.append(ship_line)
    if d.get("shipping_estimate"):
        eta_line = f"Estimated delivery: {d['shipping_estimate']}"
        if d.get("ship_days_min") and d.get("ship_days_max"):
            eta_line += f" ({d['ship_days_min']}–{d['ship_days_max']} days)"
        lines.append(eta_line)
    if d.get("ship_from"):
        lines.append(f"Ships from: {d['ship_from']}")
    return "\n".join(lines)


_pack_size = deals.pack_size


def _pdp_offer(item_id: str) -> Optional[dict]:
    """
    Everything a buying decision needs from one product page: variants with
    price per piece, shipping to COUNTRY, Choice (combined-shipping threshold),
    store, promotions.
    """
    resp = _fetch_pdp_mtop(item_id)
    res = ((resp or {}).get("data") or {}).get("result") or {}
    sku = res.get("SKU") or {}
    paths = sku.get("skuPaths") or []
    if not paths:
        return None

    # propertyId:valueId -> (name, image, hot). Sellers often give several
    # values the same name and tell them apart only by picture.
    names: dict[str, tuple[str, Optional[str], bool]] = {}
    for prop in sku.get("skuProperties") or []:
        pid = prop.get("skuPropertyId")
        for v in prop.get("skuPropertyValues") or []:
            names[f"{pid}:{v.get('propertyValueIdLong')}"] = (
                v.get("propertyValueDisplayName") or v.get("propertyValueDefinitionName") or v.get("propertyValueName") or "",
                v.get("skuPropertyImagePath"),
                bool(v.get("showHot")),
            )
    prices = (res.get("PRICE") or {}).get("skuIdStrPriceInfoMap") or {}
    qty = ((res.get("QUANTITY_PC") or {}).get("allSkuQuantityView")) or {}

    variants = []
    for n, pth in enumerate(paths, 1):
        sid = str(pth.get("skuIdStr") or pth.get("skuId"))
        parts = [names.get(part, (part, None, False)) for part in (pth.get("path") or "").split(";") if part]
        label = " / ".join(n for n, _, _ in parts) or "default"
        image = next((img for _, img, _ in parts if img), None)
        hot = any(h for _, _, h in parts)
        info = prices.get(sid) or {}
        price = parse_price(info.get("salePriceString") or "")
        pack = _pack_size(label)
        variants.append({
            "sku_id": sid,
            "label": label,
            "price": price,
            "pack": pack,
            "unit": (price / pack) if price else None,
            "max_per_order": (qty.get(sid) or {}).get("maxBuyCount", pth.get("skuStock")),
            "salable": pth.get("salable", True),
            "image": image,
            "hot": hot,
            "n": n,
        })
    counts: dict[str, int] = {}
    for v in variants:
        counts[v["label"]] = counts.get(v["label"], 0) + 1
    for v in variants:
        v["ambiguous"] = counts[v["label"]] > 1

    shipping: dict[str, Any] = {}
    layouts = (res.get("SHIPPING") or {}).get("deliveryLayoutInfo") or []
    if layouts:
        b = layouts[0].get("bizData") or {}
        free = b.get("shippingFee") == "free"
        shipping = {
            "ship_to": b.get("shipToCode"),
            "fee": 0.0 if free else b.get("displayAmount"),
            "free": free,
            "company": b.get("company") or b.get("deliveryProviderName"),
            "days": (b.get("deliveryDayMin"), b.get("deliveryDayMax")),
            "choice_threshold": parse_price(b.get("logisticsComposeThreshold") or ""),
        }

    promos = []
    for v in (res.get("PRICE_EXTEND") or {}).get("priceExtendViews") or []:
        if v.get("content"):
            promos.append(v["content"])
    banner = (res.get("PRICE_BANNER") or {}).get("targetSkuBanner") or {}
    if banner.get("supplementaryText"):
        promos.append(re.sub(r"<[^>]+>", "", banner["supplementaryText"]))
    if (res.get("COUPON_BLOCK_PC") or {}) and not (res.get("COUPON_BLOCK_PC") or {}).get("hideCouponBlock"):
        promos.append("coupons available on the product page")

    shop = res.get("SHOP_CARD_PC") or {}
    return {
        "item_id": item_id,
        "title": ((res.get("PRODUCT_TITLE") or {}).get("text")) or item_id,
        "url": f"{BASE_URL}/item/{item_id}.html",
        "variants": variants,
        "shipping": shipping,
        "choice": bool(shipping.get("choice_threshold")),
        "store": shop.get("storeName"),
        "store_positive": shop.get("sellerPositiveRate"),
        "promos": promos,
        "delivery_ext": (res.get("SHIPPING") or {}).get("deliveryExt"),
    }


def _freight(offer: dict, sku_id: Optional[str] = None) -> Optional[dict]:
    """Shipping as A + B·(qty-1) for a non-Choice offer (None for Choice)."""
    if offer.get("choice"):
        return None
    try:
        return deals.freight_quote(
            mtop_call, offer["item_id"], COUNTRY, CURRENCY, offer.get("delivery_ext"),
            offer["url"], sku_id=sku_id,
        )
    except Exception as e:
        logger.debug("freight quote %s failed: %s", offer["item_id"], e)
        return None


def _fmt_shipping(sh: dict) -> str:
    if not sh:
        return "shipping: unknown"
    days = sh.get("days") or (None, None)
    when = f", {days[0]}–{days[1]} days" if days[0] else ""
    if sh.get("choice_threshold"):
        fee = "free" if sh.get("free") else f"{sh.get('fee')} {CURRENCY}"
        return f"shipping to {sh.get('ship_to')}: Choice — {fee}, free once Choice items reach {sh['choice_threshold']:.2f} {CURRENCY}{when}"
    fee = "free" if sh.get("free") else f"{sh.get('fee')} {CURRENCY}"
    return f"shipping to {sh.get('ship_to')}: {fee} ({sh.get('company')}){when}"


@mcp.tool()
def get_product_variants(item_id: str, match: str = "", limit: int = 10) -> str:
    """
    List the variants (SKUs) of a product with price, price per piece,
    purchase limit and sku_id (cheapest per piece first), plus shipping to
    the configured country, Choice threshold, store and promotions.
    Pack size is read from the variant name ("5PCS", "10 Stück"); the
    "pack ladder" line compares pack sizes of the same variant — bigger packs
    are often nearly free, sometimes dearer per piece. Variant names can be
    identical while the article differs: look at get_variant_images (#n match).

    Args:
        item_id: AliExpress item ID (e.g., "1005007655628250")
        match: Only variants whose name contains all these words (case-insensitive),
            e.g. "5V 10pcs". Empty = all.
        limit: Max variants to show (some items have 100+).
    """
    offer = _pdp_offer(item_id)
    if not offer:
        return f"No variant data for {item_id} (session expired or product gone)." + blocked_msg()
    rows = offer["variants"]
    total = len(rows)
    words = [w.lower() for w in match.split()]
    if words:
        rows = [r for r in rows if all(w in r["label"].lower() for w in words)]
    rows.sort(key=lambda r: (not r["salable"], r["unit"] if r["unit"] is not None else 1e9))

    head = f"{len(rows)} of {total} variant(s)" + (f" matching '{match}'" if words else "")
    lines = [offer["title"], offer["url"]]
    if offer.get("store"):
        lines.append(f"store: {offer['store']}" + (f" ({offer['store_positive']}% positive)" if offer.get("store_positive") else ""))
    lines.append(_fmt_shipping(offer["shipping"]))
    if offer["promos"]:
        lines.append("promotions: " + "; ".join(offer["promos"]))
    lines.append(f"{head}, cheapest per piece first:")
    for r in rows[:max(1, limit)]:
        line = f"- #{r['n']} {r['label']}"
        if r["price"] is not None:
            line += f" — {r['price']:.2f} {CURRENCY}"
            if r["pack"] > 1:
                line += f" ({r['unit']:.2f}/pc for {r['pack']})"
        if r["max_per_order"] is not None:
            line += f" · max {r['max_per_order']}/order"
        if r.get("hot"):
            line += " · bestseller"
        if not r["salable"]:
            line += " · NOT AVAILABLE"
        line += f"\n  sku_id: {r['sku_id']}"
        if r.get("ambiguous"):
            line += " · same name as another variant: compare pictures (get_variant_images)"
        lines.append(line)
    if len(rows) > limit:
        lines.append(f"… {len(rows) - limit} more (raise limit or narrow match)")
    lines += deals.ladder_lines(rows, CURRENCY)
    return "\n".join(lines)


def _fetch_image(url: str) -> Optional[bytes]:
    try:
        r = httpx.get(url + "_220x220.jpg", timeout=15.0, headers={"User-Agent": USER_AGENT})
        return r.content if r.status_code == 200 else None
    except Exception:
        return None


@mcp.tool()
def get_variant_images(item_id: str, match: str = ""):
    """
    Show every variant's picture in ONE image (numbered grid with name and
    price) plus a short table. Use it when variant names are vague or repeat
    ("N16R8 Pin Soldered" ×3 that differ only by antenna socket).

    Args:
        item_id: AliExpress item ID.
        match: Only variants whose name contains all these words.
    """
    offer = _pdp_offer(item_id)
    if not offer:
        return f"No variant data for {item_id}." + blocked_msg()
    words = [w.lower() for w in match.split()]
    rows = [v for v in offer["variants"] if all(w in v["label"].lower() for w in words)]
    table = [offer["title"][:100]] + [
        f"#{v['n']} {v['label']} — {v['price']:.2f} {CURRENCY} · sku {v['sku_id']}"
        + ("" if v["salable"] else " · NOT AVAILABLE")
        for v in rows if v.get("price") is not None
    ]
    png = deals.contact_sheet(rows, _fetch_image, CURRENCY)
    if not png:
        return "\n".join(table + ["(no variant pictures)"])
    if len(rows) > 16:
        table.append("(picture shows the first 16; narrow with match)")
    return ["\n".join(table), Image(data=png, format="jpeg")]


@mcp.tool()
def best_offer(item_id: str, pieces: int, match: str = "", choice_order_over_threshold: bool = True) -> str:
    """
    Cheapest way to get at least `pieces` pieces from one product: compares
    pack sizes and mixes (10×1, 2×5, 1×10 …) including shipping. Non-Choice
    shipping is measured as A + B per extra unit; Choice shipping counts as
    free when the Choice part of the order is over the threshold.

    Args:
        item_id: AliExpress item ID.
        pieces: How many pieces you need.
        match: Restrict to variants whose name contains all these words
            (e.g. "5V" or "N16R8 soldered").
        choice_order_over_threshold: For Choice items, whether the whole order's
            Choice part already passes the free-shipping threshold.
    """
    offer = _pdp_offer(item_id)
    if not offer:
        return f"No variant data for {item_id}." + blocked_msg()
    words = [w.lower() for w in match.split()]
    vs = [v for v in offer["variants"] if v["salable"] and all(w in v["label"].lower() for w in words)]
    if not vs:
        return f"No available variant matches '{match}'."
    quote = _freight(offer)
    ways = deals.best_ways(vs, max(1, pieces), quote, offer["choice"] and choice_order_over_threshold, limit=50)
    lines = [f"{offer['title'][:90]} ({item_id})",
             ("Choice" if offer["choice"] else deals.fmt_quote(quote, CURRENCY))]
    lines += [f"{i}. " + deals.fmt_way(w, CURRENCY) + " · " + ", ".join(f"sku {v['sku_id']}×{q}" for v, q in w["lines"])
              for i, w in enumerate(ways[:3], 1)]
    tip = deals.upsell(ways, CURRENCY)
    if tip:
        lines.append(tip)
    odd = deals.odd_one_out(ways, vs)
    if odd:
        lines.append(odd)
    lines += deals.ladder_lines(vs, CURRENCY)
    return "\n".join(lines)


@mcp.tool()
def get_product_reviews(item_id: str, pages: int = 2, only_low: bool = False, keyword: str = "") -> str:
    """
    Read buyer reviews of a product: star distribution plus review texts
    (translated to English), with the variant each buyer bought.
    Good for spotting fakes ("got INMP441 instead of ICS-43434") and duds.

    Args:
        item_id: AliExpress item ID.
        pages: Pages of 20 reviews to read (max 10).
        only_low: Only reviews with 3 stars or fewer.
        keyword: Only reviews whose text contains this word (case-insensitive).
    """
    url = "https://feedback.aliexpress.com/pc/searchEvaluation.do"
    stats = None
    total = 0
    picked = []
    with httpx.Client(timeout=20.0, headers={"User-Agent": USER_AGENT, "Referer": f"{BASE_URL}/"}) as c:
        for page in range(1, max(1, min(pages, 10)) + 1):
            try:
                d = c.get(url, params={
                    "productId": item_id, "lang": "en_US", "country": COUNTRY, "page": page,
                    "pageSize": 20, "filter": "all", "sort": "complex_default",
                }).json().get("data") or {}
            except Exception as e:
                return f"Review request failed: {e}"
            stats = stats or d.get("productEvaluationStatistic")
            total = d.get("totalNum") or total
            for e in d.get("evaViewList") or []:
                text = (e.get("buyerTranslationFeedback") or e.get("buyerFeedback") or "").strip()
                stars = round((e.get("buyerEval") or 0) / 20)
                if only_low and stars > 3:
                    continue
                if keyword and keyword.lower() not in text.lower():
                    continue
                if text or only_low:
                    picked.append((stars, e.get("evalDate"), e.get("buyerCountry"), (e.get("skuInfo") or "").strip(), text))
            if page >= (d.get("totalPage") or 1):
                break

    lines = [f"Reviews for {item_id}: {total} total"]
    if stats:
        lines.append(
            f"average {stats.get('evarageStar')}★ · 5★ {stats.get('fiveStarNum')} · 4★ {stats.get('fourStarNum')} · "
            f"3★ {stats.get('threeStarNum')} · 2★ {stats.get('twoStarNum')} · 1★ {stats.get('oneStarNum')}"
        )
    if not picked:
        lines.append("No matching reviews with text.")
    for stars, date, country, sku_info, text in picked[:40]:
        lines.append(f"- {stars}★ {date} {country} [{sku_info}] {text[:300]}")
    return "\n".join(lines)


@mcp.tool()
def plan_basket(items: list[str]) -> str:
    """
    Price a shopping list before it goes into the cart: per line price,
    Choice items vs. others, whether the Choice free-shipping threshold is
    reached, shipping for non-Choice lines, and the total.

    Args:
        items: Lines "item_id:sku_id:quantity" (sku_id from get_product_variants;
            "item_id::qty" takes the cheapest-per-piece variant).
    """
    lines_out = []
    choice_sub = 0.0
    choice_threshold = None
    choice_fee_if_below = 0.0
    other_sub = 0.0
    other_ship = 0.0
    problems = []
    for raw in items:
        parts = [x.strip() for x in str(raw).split(":")]
        if len(parts) < 2:
            problems.append(f"{raw}: expected item_id:sku_id:quantity")
            continue
        iid, sid = parts[0], parts[1]
        q = int(parts[2]) if len(parts) > 2 and parts[2] else 1
        offer = _pdp_offer(iid)
        time.sleep(1.0)
        if not offer:
            problems.append(f"{iid}: no data")
            continue
        vs = [v for v in offer["variants"] if v["salable"]]
        v = next((x for x in vs if x["sku_id"] == sid), None) if sid else min(
            (x for x in vs if x["unit"] is not None), key=lambda x: x["unit"], default=None)
        if not v or v["price"] is None:
            problems.append(f"{iid}: variant {sid or '(cheapest)'} not found or unavailable")
            continue
        if v["max_per_order"] and q > int(v["max_per_order"]):
            problems.append(f"{iid}: {q} > max {v['max_per_order']} per order")
        line_total = v["price"] * q
        sh = offer["shipping"]
        tag = "Choice" if offer["choice"] else "seller"
        tip = ""
        same = [x for x in offer["variants"] if deals.base_name(x["label"]) == deals.base_name(v["label"])]
        ways = deals.best_ways(same, v["pack"] * q, None, True, limit=50)
        if ways and ways[0]["goods"] < line_total - 0.01:
            tip = "\n  tip: same pieces cheaper as " + deals.fmt_way(ways[0], CURRENCY)
        elif deals.upsell(ways, CURRENCY):
            tip = "\n  " + deals.upsell(ways, CURRENCY)
        if offer["choice"]:
            choice_sub += line_total
            choice_threshold = max(choice_threshold or 0, sh.get("choice_threshold") or 0)
            choice_fee_if_below = max(choice_fee_if_below, sh.get("fee") or 0)
        else:
            other_sub += line_total
            quote = _freight(offer, v["sku_id"])
            line_ship = deals.shipping_for(quote, q) if quote else (sh.get("fee") or 0)
            other_ship += line_ship
            sh = dict(sh, fee=round(line_ship, 2))
        pcs = v["pack"] * q
        lines_out.append(
            f"- {q} × {v['label']} — {line_total:.2f} {CURRENCY} [{tag}]"
            + (f" ({pcs} pcs, {line_total / pcs:.2f}/pc)" if pcs > 1 else "")
            + f"\n  {offer['title'][:90]} ({iid}, sku {v['sku_id']})"
            + ("" if offer["choice"] else f"\n  {_fmt_shipping(sh)} for qty {q}")
            + tip
        )

    choice_ship = 0.0
    if choice_sub and choice_threshold and choice_sub < choice_threshold:
        choice_ship = choice_fee_if_below
    out = ["Basket:"] + lines_out + [""]
    if choice_sub and not choice_threshold:
        out.append(f"Choice items: {choice_sub:.2f} {CURRENCY} (no threshold advertised; shipping as shown per item)")
    elif choice_sub:
        status = (
            f"reached ({choice_sub:.2f} ≥ {choice_threshold:.2f}) → free shipping"
            if choice_ship == 0 else
            f"NOT reached ({choice_sub:.2f} < {choice_threshold:.2f}) → about {choice_ship:.2f} shipping; add {choice_threshold - choice_sub:.2f} to make it free"
        )
        out.append(f"Choice items: {choice_sub:.2f} {CURRENCY}, threshold {status}")
    if other_sub:
        out.append(f"Other sellers: {other_sub:.2f} {CURRENCY} + shipping {other_ship:.2f} (A + B·qty per line)")
    out.append(f"Estimated total: {choice_sub + choice_ship + other_sub + other_ship:.2f} {CURRENCY} (before coins/coupons)")
    if problems:
        out.append("Problems: " + "; ".join(problems))
    return "\n".join(out)


@mcp.tool()
def get_shipping_estimate(item_id: str) -> str:
    """
    Check shipping time and cost for a product to the configured country
    (ALIEXPRESS_COUNTRY, default DE).

    Args:
        item_id: AliExpress item ID (e.g., "1005007655628250")
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    try:
        resp = _fetch_pdp_mtop(item_id)
    except Exception as e:
        return f"MTOP call failed: {e}"

    if not resp:
        return (
            f"Could not fetch shipping data for item {item_id} — MTOP returned "
            "no usable response. Try re-saving AliExpress credentials."
        )

    d = _extract_pdp_fields(resp, item_id)
    if d.get("shipping_cost") is None and not d.get("shipping_estimate"):
        return (
            f"Shipping info not present in API response for item {item_id}. "
            "This can happen for items that need an interactive region selection."
        )

    lines = [f"Shipping to {COUNTRY} for item {item_id}:"]
    if d.get("shipping_cost") is not None:
        lines.append("  Cost: " + ("Free" if d["shipping_cost"] == 0 else f"${d['shipping_cost']:.2f} {CURRENCY}"))
    if d.get("shipping_estimate"):
        lines.append(f"  Estimated delivery: {d['shipping_estimate']}")
    if d.get("ship_from"):
        lines.append(f"  Ships from: {d['ship_from']}")
    return "\n".join(lines)


def _cents_to_float(amt_dict: Any) -> tuple[Optional[float], Optional[str]]:
    """AliExpress cart prices are nested dicts: {amount: {cent: 804, currencyCode: 'USD'}}."""
    if not isinstance(amt_dict, dict):
        return None, None
    amount = amt_dict.get("amount") if "amount" in amt_dict else amt_dict
    if isinstance(amount, dict):
        cent = amount.get("cent")
        code = amount.get("currencyCode")
        if isinstance(cent, (int, float)):
            return round(cent / 100.0, 2), code
    fmt = amt_dict.get("formattedAmount")
    if isinstance(fmt, str):
        return parse_price(fmt), None
    return None, None


def _extract_cart(render_response: dict) -> dict:
    """
    Pull structured cart content from a `mtop.aliexpress.trade.cart.render` v1.0
    response. The response's `data.data` is a flat block-map keyed by block ID.
    We recognize these tags:

        product_item_component  → a cart line
        store_title_component   → seller/shop heading
        summary_component       → totals / checkout summary
        cart_header_component   → the top bar (contains `count`)

    Each item block's `itemId` is the real product ID; prices live in
    `prices.children.retailPrice.amount.{cent,currencyCode}`; quantity is
    `quantity.current`; variant text is `sku.skuInfo`.
    """
    result: dict[str, Any] = {
        "items": [],
        "shops": {},              # sellerId -> shop dict
        "count": None,
        "currency": None,
        "subtotal": None,
        "shipping_fee": None,
        "total": None,
    }

    data = render_response.get("data", {})
    blocks = data.get("data", {}) if isinstance(data.get("data"), dict) else {}

    for bid, b in blocks.items():
        if not isinstance(b, dict):
            continue
        tag = b.get("tag") or ""
        fields = b.get("fields") or {}
        if not isinstance(fields, dict):
            continue

        if tag.startswith("cart_header_component"):
            c = fields.get("count")
            if c is not None:
                try:
                    result["count"] = int(c)
                except (TypeError, ValueError):
                    pass

        elif tag.startswith("store_title_component"):
            sid = fields.get("sellerId") or fields.get("shopId")
            url = fields.get("url") or ""
            if isinstance(url, str) and url.startswith("//"):
                url = "https:" + url
            result["shops"][str(sid)] = {
                "name": fields.get("title"),
                "url": url,
                "shop_id": fields.get("shopId"),
                "seller_id": sid,
            }

        elif tag.startswith("product_item_component"):
            item_id = fields.get("itemId") or fields.get("productId")
            title = fields.get("title")
            if not item_id or not title:
                continue

            item: dict[str, Any] = {
                "item_id": str(item_id),
                "title": str(title)[:200],
                "sku_info": (fields.get("sku") or {}).get("skuInfo") if isinstance(fields.get("sku"), dict) else None,
                "sku_id": fields.get("skuId"),
                "image_url": fields.get("img"),
                "url": f"{BASE_URL}/item/{item_id}.html",
                "valid": fields.get("valid", True),
                "status": fields.get("status"),
                "cart_id": fields.get("cartId"),
                "checked": bool((fields.get("checkbox") or {}).get("selected") or fields.get("checkBoxSelected")),
            }

            # Quantity
            q = fields.get("quantity") or {}
            if isinstance(q, dict):
                item["quantity"] = q.get("current") or q.get("origin") or 1
            else:
                try:
                    item["quantity"] = int(q)
                except (TypeError, ValueError):
                    item["quantity"] = 1

            # Prices: current sale price
            prices_node = fields.get("prices") or {}
            children = prices_node.get("children") if isinstance(prices_node, dict) else None
            if isinstance(children, dict):
                retail = children.get("retailPrice") or children.get("salePrice")
                if isinstance(retail, dict):
                    p, c = _cents_to_float(retail)
                    item["price"] = p
                    if c and not item.get("currency"):
                        item["currency"] = c

            # Original (strike) price
            orig = fields.get("originalPrice")
            if isinstance(orig, dict):
                fmt = orig.get("formattedAmount")
                if fmt:
                    item["original_price"] = parse_price(fmt)

            # Freight
            freight = fields.get("freightInfo") or {}
            svc_list = freight.get("availableFreightServices") if isinstance(freight, dict) else None
            if isinstance(svc_list, list) and svc_list:
                chosen = next((s for s in svc_list if s.get("chosen")), svc_list[0])
                if isinstance(chosen, dict):
                    fcost = chosen.get("freightCost") or ""
                    # Format: "Shipping: $1.99" or "Free shipping"
                    if isinstance(fcost, str):
                        if "free" in fcost.lower():
                            item["shipping_cost"] = 0.0
                        else:
                            item["shipping_cost"] = parse_price(fcost)
                    item["delivery_date"] = chosen.get("deliveryDate")

            result["items"].append(item)

        elif tag.startswith("summary_component"):
            # Real structure:
            #   fields.summary.priceMap.total
            #   fields.summary.priceDetail[*].priceMap.{subTotal, total, shippingFee}
            # Also fallback: fields.checkout.orderDetail[*].priceMap.*
            summary = fields.get("summary") or {}
            pm = summary.get("priceMap") if isinstance(summary, dict) else None
            if isinstance(pm, dict):
                t = pm.get("total")
                if isinstance(t, dict):
                    v, c = _cents_to_float(t)
                    result["total"] = v
                    if c:
                        result["currency"] = c

            # Walk priceDetail groups — they have subTotal/shippingFee per order group
            pd = summary.get("priceDetail") if isinstance(summary, dict) else None
            if isinstance(pd, list):
                for grp in pd:
                    grp_pm = grp.get("priceMap") if isinstance(grp, dict) else None
                    if not isinstance(grp_pm, dict):
                        continue
                    if result["subtotal"] is None and "subTotal" in grp_pm:
                        result["subtotal"] = _cents_to_float(grp_pm["subTotal"])[0]
                    if result["shipping_fee"] is None and "shippingFee" in grp_pm:
                        result["shipping_fee"] = _cents_to_float(grp_pm["shippingFee"])[0]
                    if result["total"] is None and "total" in grp_pm:
                        v, c = _cents_to_float(grp_pm["total"])
                        result["total"] = v
                        if c and not result.get("currency"):
                            result["currency"] = c

            # Last-ditch: checkout.orderDetail
            checkout = fields.get("checkout") or {}
            if isinstance(checkout, dict):
                for grp in checkout.get("orderDetail") or []:
                    grp_pm = grp.get("priceMap") if isinstance(grp, dict) else None
                    if not isinstance(grp_pm, dict):
                        continue
                    if result["total"] is None and "total" in grp_pm:
                        v, c = _cents_to_float(grp_pm["total"])
                        result["total"] = v
                        if c and not result.get("currency"):
                            result["currency"] = c

    return result


# ─── Cart protocol (Ultron) ─────────────────────────────────────────────────
#
# The cart page is an Ultron container. `cart.render` returns page 1 as a
# snapshot {endpoint, linkage, hierarchy, data}. Every further action (next
# page, delete, ...) goes to `cart.async` with
#   params = base64(gzip(json({endpoint, operator, linkage, hierarchy,
#                              data: {operator: block, *linkage.input: block}})))
# where `operator` is the id of the block that "fired". The response is the
# next snapshot (for pagination: the next page, not an accumulation).

CART_REFERER = f"{BASE_URL}/p/shoppingcart/index.html"
CART_MAX_PAGES = 30
# AliExpress answers FAIL_SYS_USER_VALIDATE (slider captcha) after ~30 rapid
# cart calls. mtop_call solves it (slider.py), but pacing keeps it rare.
CART_CALL_DELAY_S = float(os.environ.get("ALIEXPRESS_CART_DELAY", "2"))
_last_cart_call = 0.0


def _cart_pace() -> None:
    global _last_cart_call
    wait = _last_cart_call + CART_CALL_DELAY_S - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_cart_call = time.time()


def _cart_base_payload() -> dict[str, Any]:
    return {
        "_currency": CURRENCY,
        "shipToCountry": COUNTRY,
        "_saasRegion": "aeg",
        "locale": "en_US",
        "bizParams": json.dumps({"platformType": "DESKTOP", "pcChoiceNewCart": 1}),
    }


def _cart_check(resp: dict) -> dict:
    ret = resp.get("ret", [])
    if not any("SUCCESS" in r for r in ret):
        url = (resp.get("data") or {}).get("url", "") if isinstance(resp.get("data"), dict) else ""
        if "action=deny" in url:
            raise RuntimeError(
                "AliExpress rate limit (RGV587 deny): blocked for a while after too many "
                "calls. Try again in 10-30 minutes."
            )
        if any("USER_VALIDATE" in r for r in ret):
            raise RuntimeError(
                "AliExpress bot check (FAIL_SYS_USER_VALIDATE) and the automatic slider "
                "solver did not pass. Wait a few minutes, or open the cart in the browser "
                "and solve the slider."
            )
        raise RuntimeError(f"Cart API returned: {ret[0] if ret else ret}")
    return resp.get("data") or {}


def _cart_render(cookies: dict[str, str]) -> dict:
    """Page 1 of the cart as an Ultron snapshot."""
    _cart_pace()
    return _cart_check(mtop_call(
        "mtop.aliexpress.trade.cart.render", "1.0", {"platformType": "DESKTOP"},
        cookies=cookies, referer=CART_REFERER,
    ))


def _cart_async(state: dict, operator: str, block: dict, cookies: dict[str, str], next_page: bool = False) -> dict:
    """Fire `operator` (with its possibly modified `block`) against snapshot `state`."""
    blocks = state.get("data") or {}
    linkage = state.get("linkage") or {}
    data = {operator: block}
    for k in linkage.get("input") or []:
        if k in blocks and k not in data:
            data[k] = blocks[k]
    upstream = {
        "endpoint": state.get("endpoint"),
        "operator": operator,
        "linkage": linkage,
        "data": data,
        "hierarchy": state.get("hierarchy"),
    }
    params = base64.b64encode(
        gzip.compress(json.dumps(upstream, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    ).decode("ascii")
    payload = {"compress": True, "params": params, "nextPage": next_page, **_cart_base_payload()}
    _cart_pace()
    return _cart_check(mtop_call(
        "mtop.aliexpress.trade.cart.async", "1.0", payload,
        cookies=cookies, referer=CART_REFERER,
    ))


def _cart_has_more(state: dict) -> bool:
    pg = (state.get("data") or {}).get("pagination_page") or {}
    return bool((pg.get("fields") or {}).get("hasMore"))


def _cart_next_page(state: dict, cookies: dict[str, str]) -> dict:
    return _cart_async(state, "pagination_page", state["data"]["pagination_page"], cookies, next_page=True)


def _cart_pages(cookies: dict[str, str]):
    """Yield every cart page snapshot, starting with page 1."""
    state = _cart_render(cookies)
    yield state
    for _ in range(CART_MAX_PAGES):
        if not _cart_has_more(state):
            return
        state = _cart_next_page(state, cookies)
        yield state


def _cart_item_blocks(state: dict) -> dict[str, dict]:
    """cartId (str) -> product block id, for the items on this snapshot."""
    out = {}
    for bid, b in (state.get("data") or {}).items():
        if isinstance(b, dict) and (b.get("tag") or "").startswith("product_item_component"):
            cid = (b.get("fields") or {}).get("cartId")
            if cid is not None:
                out[str(cid)] = bid
    return out


@mcp.tool()
def view_cart() -> str:
    """
    List the whole AliExpress cart (all pages; read-only).

    Each line shows [x] when the item is checked for checkout, plus its cart_id,
    which remove_from_cart takes. Subtotal/total cover the checked items only.
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    try:
        pages = list(_cart_pages(cookies))
    except Exception as e:
        return f"Cart MTOP call failed: {e}"

    cart = _extract_cart({"data": pages[0]})
    seen = {it["cart_id"] for it in cart["items"]}
    for page in pages[1:]:
        more = _extract_cart({"data": page})
        cart["shops"].update(more["shops"])
        for it in more["items"]:
            if it["cart_id"] not in seen:
                seen.add(it["cart_id"])
                cart["items"].append(it)
    items = cart["items"]
    count = cart["count"]

    if not items:
        if count == 0:
            return (
                "Your AliExpress cart is empty (server count: 0). "
                "If you just added items, re-save AliExpress cookies via the "
                "MCP Auth Bridge extension to refresh the session, then retry."
            )
        return (
            f"Cart render succeeded but no item blocks matched. Server count: {count}. "
            "The cart-line block shape may have changed."
        )

    currency = cart.get("currency") or CURRENCY
    n_checked = sum(1 for it in items if it.get("checked"))
    lines = [f"Cart ({len(items)} item(s), {n_checked} checked, server count: {count}):"]
    lines.append("")
    for it in items:
        line = f"- [{'x' if it.get('checked') else ' '}] {it['title']}"
        if it.get("price") is not None:
            line += f" — {it['price']:.2f} {it.get('currency') or currency}"
        if it.get("original_price") and it["original_price"] > (it.get("price") or 0):
            line += f" (was {it['original_price']:.2f})"
        qty = it.get("quantity") or 1
        try:
            if int(qty) != 1:
                line += f" × {qty}"
        except (TypeError, ValueError):
            pass
        if it.get("sku_info"):
            line += f"\n  variant: {it['sku_info']}"
        if it.get("shipping_cost") is not None:
            ship_str = "Free" if it["shipping_cost"] == 0 else f"{it['shipping_cost']:.2f}"
            line += f"\n  shipping: {ship_str}"
        if it.get("delivery_date"):
            line += f"\n  delivery: {it['delivery_date']}"
        if not it.get("valid", True):
            line += "\n  ⚠️ invalid (sold out or removed)"
        line += f"\n  item_id: {it['item_id']}  cart_id: {it['cart_id']}"
        lines.append(line)

    # Totals
    if cart.get("subtotal") is not None or cart.get("shipping_fee") is not None or cart.get("total") is not None:
        lines.append("")
        lines.append("Totals (checked items only):")
        if cart.get("subtotal") is not None:
            lines.append(f"Subtotal: {cart['subtotal']:.2f} {currency}")
        if cart.get("shipping_fee") is not None:
            lines.append(f"Shipping: {cart['shipping_fee']:.2f} {currency}")
        if cart.get("total") is not None:
            lines.append(f"Estimated total: {cart['total']:.2f} {currency}")

    return "\n".join(lines)


@mcp.tool()
def add_to_cart(item_id: str, sku_id: str, quantity: int = 1) -> str:
    """
    Add a product variant to the AliExpress cart. This changes the real cart
    (it does not buy anything).

    Args:
        item_id: AliExpress item ID.
        sku_id: Variant from get_product_variants (required; picks pack size/color).
        quantity: How many of that variant.
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG
    if quantity < 1:
        return "quantity must be at least 1."
    payload = {
        "locale": "en_US",
        "shipToCountry": COUNTRY,
        "state": "",
        "city": "",
        "currency": CURRENCY,
        "_saasRegion": "AEG",
        "addFrom": "",
        "addItems": json.dumps([{
            "bundleCode": None, "logisticService": None, "itemId": str(item_id),
            "toRemovedAttributeKeys": [], "quantity": int(quantity), "subItems": [],
            "bundleId": 0, "attributes": {"carAdditionalInfo": None, "warranty_ext": None},
            "skuId": str(sku_id), "bundleSellerId": 0, "channelCode": None,
        }]),
    }
    _cart_pace()
    try:
        resp = mtop_call(
            "mtop.aliexpress.trade.cart.add", "1.0", payload,
            cookies=cookies, referer=f"{BASE_URL}/item/{item_id}.html", method="POST",
        )
        _cart_check(resp)
    except Exception as e:
        return f"Add to cart failed: {e}"
    data = resp.get("data") or {}
    if data.get("addFailed"):
        return f"Add to cart refused: {data.get('errorMessage') or data.get('failReason') or 'addFailed'}"
    return (
        f"Added {quantity} × item {item_id} (sku {sku_id}) to the cart. "
        f"cart_id: {data.get('cartId')}, cart now holds {data.get('cartNum')} line(s)."
    )


@mcp.tool()
def update_cart_quantity(cart_id: str, quantity: int) -> str:
    """
    Set the quantity of one cart line. This changes the real cart.

    Args:
        cart_id: cart_id from view_cart.
        quantity: New quantity (>= 1; use remove_from_cart to delete).
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG
    if quantity < 1:
        return "quantity must be at least 1 (use remove_from_cart to delete a line)."
    cid = str(cart_id).strip()
    try:
        for state in _cart_pages(cookies):
            bid = _cart_item_blocks(state).get(cid)
            if not bid:
                continue
            block = json.loads(json.dumps(state["data"][bid]))
            fields = block.setdefault("fields", {})
            q = fields.get("quantity")
            if isinstance(q, dict):
                q["current"] = int(quantity)
            else:
                fields["quantity"] = {"current": int(quantity)}
            fields["operationType"] = "update_quantity"
            new_state = _cart_async(state, bid, block, cookies)
            response = (new_state.get("global") or {}).get("response") or {}
            if response.get("success") is False:
                return f"Refused: {response.get('tip') or 'update_quantity failed'}"
            nb = (new_state.get("data") or {}).get(bid) or {}
            nq = ((nb.get("fields") or {}).get("quantity") or {}).get("current")
            return f"cart_id {cid}: quantity now {nq if nq is not None else quantity}."
    except Exception as e:
        return f"Cart call failed: {e}"
    return f"cart_id {cid} not found in cart."


@mcp.tool()
def remove_from_cart(cart_ids: list[str]) -> str:
    """
    Remove items from the AliExpress cart. This changes the real cart.

    Args:
        cart_ids: cart_id values from view_cart (not item_ids). Confirm the exact
            list with the user before calling.
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    todo = {str(c).strip() for c in cart_ids if str(c).strip()}
    removed: list[str] = []
    failed: dict[str, str] = {}

    # Each item can only be deleted from the snapshot of the page it is on.
    # Walk the pages; on the first page holding targets, delete them one by one
    # (each response is the new snapshot). When a target is no longer in the
    # newest snapshot, start over from page 1 because the pages have shifted.
    # A pass that attempted anything (deleted or rejected) counts as progress,
    # so a rejected item never stops the walk to later pages.
    for _ in range(len(todo) + CART_MAX_PAGES):
        if not todo:
            break
        progressed = False
        try:
            for state in _cart_pages(cookies):
                targets = [c for c in _cart_item_blocks(state) if c in todo]
                if not targets:
                    continue
                for cid in targets:
                    bid = _cart_item_blocks(state).get(cid)
                    if bid is None:
                        break  # snapshot moved on; re-walk from page 1
                    progressed = True
                    block = json.loads(json.dumps(state["data"][bid]))
                    block.setdefault("fields", {})["operationType"] = "delete"
                    try:
                        state = _cart_async(state, bid, block, cookies)
                    except Exception as e:
                        failed[cid] = str(e)
                        todo.discard(cid)
                        continue
                    todo.discard(cid)
                    response = (state.get("global") or {}).get("response") or {}
                    if response.get("success") is False:
                        failed[cid] = response.get("tip") or "rejected"
                        continue
                    removed.append(cid)
                break
            else:
                break  # walked every page, nothing left to find
        except Exception as e:
            return f"Cart call failed after removing {len(removed)} item(s): {e}"
        if not progressed:
            break

    lines = [f"Removed {len(removed)} item(s)."]
    if failed:
        lines.append("Failed: " + "; ".join(f"{c}: {m}" for c, m in failed.items()))
    if todo:
        lines.append("Not found in cart: " + ", ".join(sorted(todo)))
    return "\n".join(lines)


# ─── Main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
