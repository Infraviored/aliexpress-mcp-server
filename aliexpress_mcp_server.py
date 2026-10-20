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

import browser_actions
import cart as cartlib
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
    captcha: bool = True,
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
            return mtop_call(api, version, payload, cookies=new_cookies, retries=retries - 1, referer=referer, method=method, captcha=captcha)

        # Bot check: solve the slider (or click the reCAPTCHA box) in real Chrome
        # on a virtual display, retry once.
        punish_url = (data.get("data") or {}).get("url") if isinstance(data.get("data"), dict) else None
        if "FAIL_SYS_USER_VALIDATE" in ret_str:
            BOT_CHECK["since"] = BOT_CHECK.get("since") or time.time()
        # `action=deny` (after hammering past a solved slider) has no slider: wait it out.
        if captcha and punish_url and "action=captcha" in punish_url:
            import slider
            logger.info("MTOP bot check on %s, solving slider", api)
            try:
                solved = slider.solve(punish_url, CREDENTIALS_PATH)
            except Exception as e:
                logger.warning("slider solver failed: %s", e)
                solved = False
            if solved:
                BOT_CHECK.clear()
                # Passing the check sets a new token: allow one token refresh, no second captcha.
                return mtop_call(api, version, payload, cookies=load_cookies(), retries=1, referer=referer, method=method, captcha=False)
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
    choice_only: bool = False,
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
        choice_only: Only "Shipped by AliExpress" (Choice) offers: about a
            week to DE, free shipping above the Choice threshold.
    """
    try:
        products = _search(query, sort_by, page, choice_only)
    except PermissionError:
        return AUTH_EXPIRED_MSG
    if min_rating > 0:
        products = [p for p in products if p.get("rating") and p["rating"] >= min_rating]
    if max_price > 0:
        products = [p for p in products if p.get("price") is not None and p["price"] <= max_price]

    if not products:
        filters = []
        if min_rating > 0:
            filters.append(f"rating ≥ {min_rating}")
        if max_price > 0:
            filters.append(f"price ≤ {max_price:.2f}")
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


def _search(query: str, sort_by: str = "best_match", page: int = 1, choice_only: bool = False) -> list[dict]:
    """
    Search results as dicts (item_id, title, price, rating, orders, choice, ...).
    `choice_only`: AliExpress's own "Choice" filter (selectedSwitches
    filterCode:choice_atm).
    """
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
    if choice_only:
        params["SearchText"] = query
        params["selectedSwitches"] = "filterCode:choice_atm"
    client = get_client()
    try:
        resp = client.get(url_path, params=params)
        if check_auth_redirect(resp):
            raise PermissionError("auth expired")
        resp.raise_for_status()
        # The filter lets the odd hit through whose card lacks the Choice
        # badge; the product page decides (shop_list checks it there).
        return parse_search_results(resp.text)
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


_DISC_CACHE: dict[str, tuple[float, dict]] = {}


def _discounts(item_id: str, sku_id: str = "") -> Optional[dict]:
    """Store discount tiers, coupons and coins from the product's discount panel."""
    hit = _DISC_CACHE.get(item_id)
    if hit and time.time() - hit[0] < PDP_TTL:
        return hit[1]
    try:
        resp = mtop_call("mtop.aliexpress.pdp.promotion.panel", "1.0", {
            "productId": item_id, "skuId": sku_id, "quantity": "1", "targetPanel": "global",
            "channel": "", "sourceType": "pc", "clientType": "pc", "recBizScene": "",
            "discountExt": "", "__inline": "true", "_lang": f"en_{COUNTRY}", "_currency": CURRENCY,
        }, referer=f"{BASE_URL}/item/{item_id}.html")
    except Exception as e:
        logger.debug("discount panel %s failed: %s", item_id, e)
        return None
    if not any("SUCCESS" in r for r in resp.get("ret") or []):
        return None
    d = deals.parse_discounts(resp)
    # The panel never marks public code coupons as collected; remember ours.
    mine = _collected_coupons()
    for c in d["coupons"]:
        if c["how"] == "collect" and c.get("campaign_id") in mine:
            c["how"] = "collected"
    _DISC_CACHE[item_id] = (time.time(), d)
    return d


COUPONS_FILE = CREDENTIALS_PATH.parent / "aliexpress-coupons.json"


def _collected_coupons() -> set[str]:
    try:
        return set(json.loads(COUPONS_FILE.read_text()))
    except (OSError, ValueError):
        return set()


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
    pack sizes and mixes (10×1, 2×5, 1×10 …) including shipping and the
    store's discounts/coupons (ranked by net price; lists every discount). Non-Choice
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
    disc = _discounts(item_id, vs[0]["sku_id"])
    for w in ways:
        w["save"], w["save_how"] = deals.discount_for(disc, w["goods"])
        w["net"] = w["total"] - w["save"]
    ways.sort(key=lambda w: (w["net"], -w["pieces"]))
    lines = [f"{offer['title'][:90]} ({item_id})",
             ("Choice" if offer["choice"] else deals.fmt_quote(quote, CURRENCY))]
    lines += [f"{i}. " + deals.fmt_way(w, CURRENCY)
              + (f" − {w['save']:.2f} ({w['save_how']}) = {w['net']:.2f} net" if w["save"] else "")
              + " · " + ", ".join(f"sku {v['sku_id']}×{q}" for v, q in w["lines"])
              for i, w in enumerate(ways[:3], 1)]
    if ways:
        more = deals.next_discount(disc, ways[0]["goods"], CURRENCY)
        if more:
            lines.append("discount tip: " + more)
    lines += deals.fmt_discounts(disc, CURRENCY)
    tip = deals.upsell(ways, CURRENCY)
    if tip:
        lines.append(tip)
    odd = deals.odd_one_out(ways, vs)
    if odd:
        lines.append(odd)
    lines += deals.ladder_lines(vs, CURRENCY)
    return "\n".join(lines)


@mcp.tool()
def get_discounts(item_id: str, subtotal: float = 0) -> str:
    """
    The product's "Discounts & coupon codes": automatic store discount tiers,
    store coupons (collect / follow / code), coins. With `subtotal` (your
    order total in this store) it says what applies and what the next tier
    would give. best_offer and plan_basket already apply these.

    Args:
        item_id: AliExpress item ID.
        subtotal: Goods total in this store, to compute the saving.
    """
    d = _discounts(item_id)
    if d is None:
        return f"No discount data for {item_id}." + blocked_msg()
    lines = deals.fmt_discounts(d, CURRENCY) or ["No store discounts or coupons."]
    if subtotal:
        off, how = deals.discount_for(d, subtotal)
        lines.append(f"at {subtotal:.2f}: −{off:.2f}" + (f" ({how})" if how else ""))
        more = deals.next_discount(d, subtotal, CURRENCY)
        if more:
            lines.append(more)
    return "\n".join(lines)


def _collect(item_id: str, coupons: list[dict]) -> dict[str, str]:
    res = browser_actions.collect_coupons(item_id, [c["campaign_id"] for c in coupons], CREDENTIALS_PATH)
    got = {cid for cid, r in res.items() if r == "collected"}
    if got:
        COUPONS_FILE.write_text(json.dumps(sorted(_collected_coupons() | got)))
        os.chmod(COUPONS_FILE, 0o600)
    _DISC_CACHE.pop(item_id, None)
    return res


@mcp.tool()
def collect_coupon(item_id: str, subtotal: float) -> str:
    """
    Collect the store coupon worth having for an order of `subtotal` in this
    product's store (only plain "Collect" coupons; never "Follow & collect").
    Runs the product page in a browser, because AliExpress accepts coupon
    collection only from its own page. Costs nothing; one coupon applies per
    store and order.

    Args:
        item_id: Any product of the store.
        subtotal: The goods total you will order in this store.
    """
    d = _discounts(item_id)
    if d is None:
        return f"No discount data for {item_id}." + blocked_msg()
    c = deals.coupon_to_collect(d, subtotal)
    if not c:
        off, how = deals.discount_for(d, subtotal, coupons=False)
        return f"Nothing to collect: at {subtotal:.2f} the best usable discount is −{off:.2f} ({how or 'none'})."
    try:
        res = _collect(item_id, [c])
    except Exception as e:
        return f"Collecting failed: {e}"
    code = f" (code {c['code']}; the checkout page shows whether it applies)" if c.get("code") else ""
    return f"Coupon {c['off']:.2f} off over {c['min']:.2f}: {res.get(c['campaign_id'])}{code}."


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


# ─── Shopping list → plan → cart ─────────────────────────────────────────────

# A day of delivery is worth this much (EUR) when comparing offers, so a
# Choice offer arriving in 8 days beats a global seller's 22 days unless the
# latter is clearly cheaper.
DAY_COST = float(os.environ.get("ALIEXPRESS_DAY_COST", "0.15"))
UNKNOWN_DAYS = 30


def _words(text: str) -> list[str]:
    return [w.lower() for w in (text or "").split()]


def _choice_only(spec: dict) -> bool:
    return str(spec.get("seller", "any")).lower() == "choice"


def _candidate_ids(spec: dict) -> list[str]:
    """Given item_ids plus the top search hits for `query` (both compete)."""
    ids = [str(x) for x in spec.get("item_ids") or []]
    if not spec.get("query"):
        return ids
    avoid = _words(spec.get("avoid", ""))
    hits = [h for h in _search(spec["query"], spec.get("sort_by", "best_match"), choice_only=_choice_only(spec))
            if not any(w in (h.get("title") or "").lower() for w in avoid)]
    for h in hits[: int(spec.get("candidates", 3))]:
        if h["item_id"] not in ids:
            ids.append(h["item_id"])
    return ids


def _ways_for(item_id: str, spec: dict, day_cost: float) -> tuple[list[dict], list[str]]:
    """Ranked ways to cover `spec` from one product, and notes about it."""
    offer = _pdp_offer(item_id)
    if not offer:
        return [], [f"{item_id}: no product data" + blocked_msg()]
    if _choice_only(spec) and not offer["choice"]:
        return [], [f"{item_id}: not Choice (global seller) — skipped, Choice only was asked"]
    pieces = max(1, int(spec.get("pieces", 1)))
    match, avoid = _words(spec.get("match", "")), _words(spec.get("avoid", ""))
    # A match word may stand in the variant name or, for every variant, in
    # the title ("ESP32 S3 N16R8 CAM …" has the single variant "default").
    title = (offer.get("title") or "").lower()
    ok = [v for v in offer["variants"] if v["salable"] and v["price"] is not None
          and not any(w in v["label"].lower() for w in avoid)]
    vs = [v for v in ok if all(w in v["label"].lower() for w in match)]
    if not vs and not any(w in v["label"].lower() for v in offer["variants"] for w in match):
        # No variant names it at all: the title speaks for every variant.
        vs = [v for v in ok if all(w in title for w in match)]
    if spec.get("sku_id"):
        vs = [v for v in offer["variants"] if v["sku_id"] == str(spec["sku_id"])]
    if not vs:
        return [], [f"{item_id}: no available variant matches '{spec.get('match', '')}'"]
    quote = _freight(offer, vs[0]["sku_id"])
    if spec.get("sku_id") and spec.get("qty"):
        q = int(spec["qty"])
        ship = 0.0 if offer["choice"] else deals.shipping_for(quote, q)
        ways = [{"lines": [(vs[0], q)], "line_ship": [ship], "pieces": vs[0]["pack"] * q,
                 "goods": vs[0]["price"] * q, "ship": ship, "total": vs[0]["price"] * q + ship}]
    else:
        ways = deals.best_ways(vs, pieces, quote, offer["choice"], limit=10)
    days = (quote or {}).get("days") or offer["shipping"].get("days") or (None, None)
    dmax = days[1] if days and days[1] else None
    disc = _discounts(item_id, vs[0]["sku_id"])
    for w in ways:
        # Rank on what applies by itself; coupons someone must collect are shown apart.
        w["save"], w["save_how"] = deals.discount_for(disc, w["goods"], coupons=False)
        full, full_how = deals.discount_for(disc, w["goods"])
        w["coupon"] = (round(full - w["save"], 2), full_how) if full > w["save"] + 0.005 else None
        w["net"] = w["total"] - w["save"]
        w["days"] = dmax
        w["score"] = w["net"] + day_cost * (dmax if dmax is not None else UNKNOWN_DAYS)
        w.update(offer=offer, quote=quote, disc=disc)
    ways.sort(key=lambda w: (w["score"], len(w["lines"])))
    notes = []
    # A winner far cheaper than every other variant is usually another
    # article (bare chip, adapter): rank it out, say so.
    for _ in range(3):
        if not ways or not deals.odd_one_out(ways, vs):
            break
        bad = {v["label"] for v, _ in ways[0]["lines"]}
        ways = [w for w in ways if not any(v["label"] in bad for v, _ in w["lines"])]
        notes.append(f"{item_id}: skipped variant {', '.join(sorted(bad))} — far cheaper than the others, "
                     "probably an accessory or bare chip (get_variant_images to check)")
    if ways and any(v.get("ambiguous") for v, _ in ways[0]["lines"]):
        notes.append(f"! variant names repeat — look at get_variant_images({item_id}) before trusting the pick")
    return ways, notes


def _plan(specs: list[dict], day_cost: float = DAY_COST) -> dict:
    """Best offer per list item, then the basket: Choice threshold, store discounts, delivery."""
    picks, notes, candidates = [], [], set()
    for spec in specs:
        need = spec.get("need") or spec.get("query") or ", ".join(map(str, spec.get("item_ids") or [])) or "?"
        try:
            ids = _candidate_ids(spec)
        except PermissionError:
            raise
        except Exception as e:
            notes.append(f"{need}: search failed: {e}")
            continue
        candidates.update(ids)
        options = []
        for iid in ids:
            ways, n = _ways_for(iid, spec, day_cost)
            notes += [f"{need}: {x}" for x in n]
            options += ways[:3]
        options.sort(key=lambda w: w["score"])
        if not options:
            notes.append(f"{need}: nothing usable found")
            continue
        picks.append({"need": need, "spec": spec, "options": options})

    # The parcel is complete when its slowest part arrives: a faster offer is
    # only worth more where it would otherwise be the slowest part.
    for p in picks:
        others = [q["options"][0]["days"] or UNKNOWN_DAYS for q in picks if q is not p]
        floor = max(others, default=0)
        for w in p["options"]:
            w["score"] = w["net"] + day_cost * max(w["days"] or UNKNOWN_DAYS, floor)
        p["options"].sort(key=lambda w: (w["score"], w["days"] or UNKNOWN_DAYS, len(w["lines"])))
        best = p["options"][0]
        seen = {best["offer"]["item_id"]}
        p["others"] = []
        for w in p["options"][1:]:
            if w["offer"]["item_id"] not in seen and len(p["others"]) < 2:
                seen.add(w["offer"]["item_id"])
                p["others"].append(w)
        p["way"] = best

    choice = [p for p in picks if p["way"]["offer"]["choice"]]
    choice_goods = sum(p["way"]["goods"] for p in choice)
    threshold = max((p["way"]["offer"]["shipping"].get("choice_threshold") or 0 for p in choice), default=0)
    choice_fee = 0.0
    if choice and threshold and choice_goods < threshold:
        choice_fee = max((p["way"]["offer"]["shipping"].get("fee") or 0) for p in choice)
    stores: dict[str, dict] = {}
    for p in picks:
        w = p["way"]
        key = str((w["disc"] or {}).get("seller_id") or w["offer"].get("store") or w["offer"]["item_id"])
        st = stores.setdefault(key, {"name": w["offer"].get("store") or key, "sub": 0.0, "disc": w["disc"],
                                     "item_id": w["offer"]["item_id"]})
        st["sub"] += w["goods"]
    saved = coupons = 0.0
    for st in stores.values():
        st["save"], st["how"] = deals.discount_for(st["disc"], st["sub"], coupons=False)
        full, st["coupon_how"] = deals.discount_for(st["disc"], st["sub"])
        st["coupon"] = round(full - st["save"], 2)
        st["more"] = deals.next_discount(st["disc"], st["sub"], CURRENCY)
        saved += st["save"]
        coupons += st["coupon"]
    goods = sum(p["way"]["goods"] for p in picks)
    ship = sum(p["way"]["ship"] for p in picks) + choice_fee
    dated = [p for p in picks if p["way"]["days"]]
    slowest = max(dated, key=lambda p: p["way"]["days"]) if dated else None
    return {
        "picks": picks, "notes": notes, "candidates": candidates,
        "choice_goods": choice_goods, "threshold": threshold, "choice_fee": choice_fee,
        "stores": stores, "goods": goods, "ship": ship, "saved": saved, "coupons": coupons,
        "total": goods + ship - saved, "slowest": slowest,
    }


def _count_cart_choice(cookies: dict[str, str], plan: dict) -> None:
    """
    Choice ships free per order: checked Choice lines already in the cart
    (other products) count towards the threshold together with the plan.
    """
    try:
        cart = _read_cart(cookies)
    except Exception as e:
        plan["notes"].append(f"could not read the cart for the Choice threshold: {e}")
        return
    other = sum((ln["price"] or 0) * ln["qty"] for ln in cart["lines"]
                if ln["checked"] and ln["group"] == "group_choice" and ln["item_id"] not in plan["candidates"])
    plan["cart_choice"] = round(other, 2)
    if other + plan["choice_goods"] >= plan["threshold"]:
        plan["ship"] -= plan["choice_fee"]
        plan["total"] -= plan["choice_fee"]
        plan["choice_fee_waived"] = plan["choice_fee"]
        plan["choice_fee"] = 0.0


def _fmt_plan(plan: dict) -> str:
    cur = CURRENCY
    out = [f"Plan: {len(plan['picks'])} item(s), "
           f"{sum(len(p['way']['lines']) for p in plan['picks'])} cart line(s)"]
    for i, p in enumerate(plan["picks"], 1):
        w, o = p["way"], p["way"]["offer"]
        kind = "Choice" if o["choice"] else f"global seller, {len(w['lines'])} line(s) × own shipping"
        parts = " + ".join(f"{q}× {v['label']}" for v, q in w["lines"])
        ship = "free" if o["choice"] else f"{w['ship']:.2f}"
        out.append(f"{i}. {p['need']}: {parts} = {w['pieces']} pcs — {o['title'][:60]} ({o['item_id']})")
        out.append(f"   goods {w['goods']:.2f} + shipping {ship}"
                   + (f" − {w['save']:.2f} ({w['save_how']})" if w["save"] else "")
                   + f" = {w['net']:.2f} {cur} · {w['days'] or '?'} days · {kind} · {o.get('store') or ''}"
                   + (f"\n   coupon to collect: −{w['coupon'][0]:.2f} ({w['coupon'][1]})" if w.get("coupon") else ""))
        for alt in p["others"]:
            ao = alt["offer"]
            out.append(f"   vs {ao['title'][:45]} ({ao['item_id']}): {alt['net']:.2f} {cur}, "
                       f"{alt['days'] or '?'} days{' Choice' if ao['choice'] else ''}"
                       f" → {alt['score'] - w['score']:+.2f} incl. delivery time")
    out.append("")
    if plan["threshold"]:
        if plan.get("choice_fee_waived"):
            st = (f"free shipping together with the checked Choice lines already in the cart "
                  f"({plan['cart_choice']:.2f}); alone it would cost {plan['choice_fee_waived']:.2f}")
        elif plan["choice_fee"]:
            short = plan["threshold"] - plan["choice_goods"] - plan.get("cart_choice", 0)
            st = f"{plan['choice_fee']:.2f} shipping — add {short:.2f} Choice goods to make it free"
        else:
            st = "free shipping"
        out.append(f"Choice goods {plan['choice_goods']:.2f} {cur} (threshold {plan['threshold']:.2f}): {st}")
    for st in plan["stores"].values():
        if st["save"] or st["coupon"] or st["more"]:
            out.append(f"Store {st['name']}: {st['sub']:.2f}"
                       + (f" → −{st['save']:.2f} ({st['how']})" if st["save"] else "")
                       + (f"; coupon to collect −{st['coupon']:.2f} ({st['coupon_how']})" if st["coupon"] else "")
                       + (f"; {st['more']}" if st["more"] else ""))
    out.append(f"Total ≈ {plan['goods']:.2f} goods + {plan['ship']:.2f} shipping"
               + (f" − {plan['saved']:.2f} automatic discounts" if plan["saved"] else "")
               + f" = {plan['total']:.2f} {cur}"
               + (f"; {plan['total'] - plan['coupons']:.2f} with the coupons collected" if plan["coupons"] else "")
               + " (coins not included)")
    if plan["slowest"]:
        sl = plan["slowest"]
        out.append(f"Slowest: {sl['need']} — up to {sl['way']['days']} days")
    for n in plan["notes"]:
        out.append(f"note: {n}")
    return "\n".join(out)


def _set_qty(cookies: dict[str, str], cid: str, qty: int):
    def edit(fields):
        fields["operationType"] = "update_quantity"
        fields["quantityView"] = {"current": int(qty)}
    return _cart_line_action(cookies, [cid], edit)


def _apply_plan(cookies: dict[str, str], plan: dict) -> str:
    """Put the plan into the cart, drop the MCP's own replaced lines, check exactly the plan, verify."""
    cart = _read_cart(cookies)
    by_key = {(ln["item_id"], ln["sku_id"]): ln for ln in cart["lines"]}
    want: list[str] = []
    expect: dict[str, dict] = {}
    log = []
    for p in plan["picks"]:
        w = p["way"]
        iid = w["offer"]["item_id"]
        for (v, q), ship in zip(w["lines"], w["line_ship"]):
            ln = by_key.get((iid, v["sku_id"]))
            if ln:
                cid = ln["cart_id"]
                if ln["qty"] != q:
                    done, failed = _set_qty(cookies, cid, q)
                    log.append(f"quantity {ln['qty']}→{q}: {p['need']} ({v['label']})" if done
                               else f"FAILED quantity for {p['need']}: {failed}")
            else:
                try:
                    cid = _cart_add(cookies, iid, v["sku_id"], q)
                    log.append(f"added {q}× {v['label']}: {p['need']}")
                except Exception as e:
                    log.append(f"FAILED to add {p['need']} ({v['label']}): {e}")
                    continue
            want.append(cid)
            expect[cid] = {"need": p["need"], "ship": ship, "days": w["days"], "choice": w["offer"]["choice"],
                           "price": v["price"]}
    mine = _mcp_lines()
    drop = [ln for ln in cart["lines"]
            if ln["cart_id"] in mine and ln["item_id"] in plan["candidates"] and ln["cart_id"] not in want]
    if drop:
        def edit(fields):
            fields["operationType"] = "delete"
        done, failed = _cart_line_action(cookies, [ln["cart_id"] for ln in drop], edit)
        _remember_lines(drop=done)
        for ln in drop:
            log.append(("removed (replaced by the plan): " if ln["cart_id"] in done else "FAILED to remove: ")
                       + f"{ln['title'][:40]} ({ln['variant']})")
    for st in plan["stores"].values():
        c = deals.coupon_to_collect(st["disc"], st["sub"])
        if not c:
            continue
        try:
            res = _collect(st["item_id"], [c]).get(c["campaign_id"])
        except Exception as e:
            res = f"failed: {e}"
        log.append(f"coupon {c['off']:.2f} off over {c['min']:.2f} at {st['name']}: {res}")
    sel = _select(cookies, want, only=True)
    after = _read_cart(cookies)
    lines = {ln["cart_id"]: ln for ln in after["lines"]}
    diffs = []
    for cid, e in expect.items():
        ln = lines.get(cid)
        if not ln:
            diffs.append(f"{e['need']}: line {cid} not in the cart")
            continue
        if ln["price"] is not None and e["price"] and abs(ln["price"] - e["price"]) > 0.05:
            diffs.append(f"{e['need']}: {ln['price']:.2f} each in the cart, {e['price']:.2f} on the product page")
        if ln["ship"] is not None and not e["choice"] and abs(ln["ship"] - e["ship"]) > 0.5:
            diffs.append(f"{e['need']}: shipping {ln['ship']:.2f} in the cart, planned {e['ship']:.2f}")
        days = ln["days"][1]
        if days and e["days"] and abs(int(days) - int(e["days"])) > 3:
            diffs.append(f"{e['need']}: {days} days in the cart, planned {e['days']}")
    out = ["Applied:"] + [f"- {x}" for x in log] + ["", sel, ""]
    out.append("Plan vs cart: " + ("matches (price, shipping within 0.50, delivery within 3 days)." if not diffs
                                   else "differs:\n" + "\n".join(f"- {d}" for d in diffs)))
    # The checkout link holds every plan line, also those beyond the 10 the
    # cart page keeps checked: report on all of them.
    sub = [dict(lines[c], checked=True) for c in want if c in lines]
    goods = sum((ln["price"] or 0) * ln["qty"] for ln in sub)
    ship = sum(ln["ship"] or 0 for ln in sub)
    out.append(f"Checkout link total ≈ goods {goods:.2f} + shipping {ship:.2f}"
               f" − store discounts {plan['saved']:.2f} = {goods + ship - plan['saved']:.2f} {CURRENCY}"
               " (the checkout page has the final figure)")
    r = cartlib.shipping_report({**after, "lines": sub})
    for ln, why in r["paid"]:
        out.append(f"- paid shipping {ln['ship']:.2f}: {ln['title'][:40]} — {why}")
    if r["slowest"]:
        out.append(f"Slowest line: {r['slowest']['title'][:40]}, arrives by {r['latest']}")
    return "\n".join(out)


@mcp.tool()
def shop_list(items: list[dict], apply: bool = False, day_cost: float = DAY_COST, choice_only: bool = False) -> str:
    """
    From a shopping list to a checked cart: finds the best offer for every
    item, prices the basket as checkout will (shipping per cart line for
    global sellers, Choice free-shipping threshold, store discounts and
    coupons, delivery time), and with apply=True puts exactly that into the
    cart. Never buys: the result ends with the checkout link for the user.

    Each item is a dict:
        need:      what it is, for the report ("ESP32-S3 N16R8 dev board")
        pieces:    how many pieces are needed (packs are counted in pieces)
        query:     search term — the top `candidates` hits (default 3) compete
        item_ids:  candidate products (compete with the search hits, if any)
        match:     words the chosen variant name or the product title must
                   contain ("N16R8 Module")
        avoid:     words that rule out a variant or search hit ("chip shell")
        sku_id + qty: take exactly this variant and quantity (no optimizing)
        seller:    "choice" = only "Shipped by AliExpress" offers (fast, free
                   above the Choice threshold); default "any" also allows
                   global sellers (often cheaper, 2–4 weeks, shipping per line)

    Ask the user first whether global sellers are fine or only Choice, unless
    they already said; pass the answer as choice_only or per item `seller`.

    Ranking: net price (goods + shipping − store discount) + day_cost per day
    of delivery. When the report says variant names repeat or a variant is
    suspiciously cheap, look at get_variant_images and set `match`.

    apply=True: adds or re-sizes the planned lines, removes lines this MCP
    added earlier for these products that the plan no longer uses (never the
    user's own lines), unchecks everything else, checks the plan (AliExpress
    keeps at most 10 checked) and returns the checkout link with all plan
    lines, then compares plan and cart (shipping, delivery).

    Args:
        items: The shopping list (see above).
        apply: Put the plan into the cart.
        day_cost: EUR per delivery day when ranking (default 0.15).
        choice_only: `seller: "choice"` for every item.
    """
    if choice_only:
        items = [{**it, "seller": "choice"} for it in items]
    try:
        plan = _plan(items, day_cost)
    except PermissionError:
        return AUTH_EXPIRED_MSG
    cookies = load_cookies()
    if plan["choice_fee"] and cookies:
        _count_cart_choice(cookies, plan)
    text = _fmt_plan(plan)
    if not apply or not plan["picks"]:
        return text
    if not cookies:
        return text + "\n\n" + AUTH_EXPIRED_MSG
    try:
        return text + "\n\n" + _apply_plan(cookies, plan)
    except Exception as e:
        return text + f"\n\nApplying failed: {e}"


@mcp.tool()
def plan_basket(items: list[str]) -> str:
    """
    Price fixed cart lines before they go into the cart (shop_list without
    searching or optimizing): shipping per line, Choice threshold, store
    discounts, delivery.

    Args:
        items: Lines "item_id:sku_id:quantity" (sku_id from get_product_variants).
    """
    specs = []
    for raw in items:
        parts = [x.strip() for x in str(raw).split(":")]
        if len(parts) < 2:
            return f"{raw}: expected item_id:sku_id:quantity"
        q = int(parts[2]) if len(parts) > 2 and parts[2] else 1
        spec = {"need": parts[0], "item_ids": [parts[0]], "qty": q}
        if parts[1]:
            spec["sku_id"] = parts[1]
        else:
            spec["pieces"] = q
        specs.append(spec)
    try:
        return _fmt_plan(_plan(specs))
    except PermissionError:
        return AUTH_EXPIRED_MSG


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


# ─── Cart protocol (droplet) ────────────────────────────────────────────────
#
# The cart page (pcChoiceNewCart=1) is a "droplet" container. `cart.render`
# returns page 1 as a snapshot {asyncHandler, config, page, data, global}.
# Every further action (next page, checkbox, quantity, delete) goes to
# `cart.async` with
#   params = base64(gzip(json({config, operator, asyncHandler, page,
#                              data: {operator: block, root: rootBlock}})))
# where `operator` is the id of the block that "fired" and the block carries
# `fields.operationType` (checkbox / update_quantity / delete). The response
# is the next snapshot; for pagination it holds the next page only. Parsing
# and the shipping analysis live in cart.py. Learned by recording the real
# page with tools/capture_mtop.py; notes in docs/mtop-notes.md.

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
                "AliExpress bot check (FAIL_SYS_USER_VALIDATE) and the automatic solver "
                "did not pass. Wait a few minutes, or open the cart in the browser once."
            )
        raise RuntimeError(f"Cart API returned: {ret[0] if ret else ret}")
    return resp.get("data") or {}


def _cart_render(cookies: dict[str, str]) -> dict:
    """Page 1 of the cart as a droplet snapshot (the web page's format, in CURRENCY)."""
    _cart_pace()
    return _cart_check(mtop_call(
        "mtop.aliexpress.trade.cart.render", "1.0",
        {"platformType": "DESKTOP", **_cart_base_payload()},
        cookies=cookies, referer=CART_REFERER,
    ))


def _cart_async(state: dict, operator: str, block: dict, cookies: dict[str, str], next_page: bool = False) -> dict:
    """Fire `operator` (with its possibly modified `block`) against snapshot `state`."""
    blocks = state.get("data") or {}
    root = (state.get("page") or {}).get("root")
    data = {operator: block}
    if root in blocks and root != operator:
        data[root] = blocks[root]
    upstream = {
        "config": state.get("config"),
        "operator": operator,
        "asyncHandler": state.get("asyncHandler"),
        "data": data,
        "page": state.get("page"),
    }
    params = base64.b64encode(
        gzip.compress(json.dumps(upstream, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    ).decode("ascii")
    payload = {"compress": True, "params": params, "nextPage": next_page, **_cart_base_payload()}
    _cart_pace()
    return _cart_check(mtop_call(
        "mtop.aliexpress.trade.cart.async", "1.0", payload,
        cookies=cookies, referer=CART_REFERER, method="POST",
    ))


def _cart_session(cookies: dict[str, str]) -> tuple[dict, list[dict]]:
    """All pages, plus one snapshot merged over them that actions are sent against."""
    pages, merged = [], None
    for state in _cart_render_all(cookies):
        pages.append(state)
        merged = cartlib.merge(merged, state)
    return merged, pages


def _cart_render_all(cookies: dict[str, str]):
    # Next pages must be requested from the merged snapshot as well.
    merged = None
    state = _cart_render(cookies)
    for _ in range(CART_MAX_PAGES + 1):
        yield state
        merged = cartlib.merge(merged, state)
        pg = cartlib.pagination(state)
        if not pg or not pg[1].get("hasMore"):
            return
        state = _cart_async(merged, pg[0], pg[1], cookies, next_page=True)


def _cart_line_action(cookies: dict[str, str], cart_ids: list[str], edit) -> tuple[list[str], dict[str, str]]:
    """
    Fire one action per cart line against the merged snapshot. `edit(fields)`
    sets operationType and the changed field on a copy of the line's block.
    Each response is folded back in, as the web page does.
    """
    merged, _ = _cart_session(cookies)
    done: list[str] = []
    failed: dict[str, str] = {}
    for cid in [str(c).strip() for c in cart_ids if str(c).strip()]:
        bid = cartlib.item_blocks(merged).get(cid)
        if bid is None:
            failed[cid] = "not found in cart"
            continue
        block = json.loads(json.dumps(merged["data"][bid]))
        edit(block.setdefault("fields", {}))
        try:
            resp = _cart_async(merged, bid, block, cookies)
        except Exception as e:
            failed[cid] = str(e)
            continue
        response = (resp.get("global") or {}).get("response") or {}
        if response.get("success") is False:
            failed[cid] = response.get("tip") or "rejected"
            continue
        merged = cartlib.merge(merged, resp)
        if (block["fields"].get("operationType")) == "delete":
            merged["data"].pop(bid, None)
            for children in (merged.get("page") or {}).get("structure", {}).values():
                if bid in children:
                    children.remove(bid)
        done.append(cid)
    return done, failed


def _read_cart(cookies: dict[str, str]) -> dict:
    return cartlib.parse(list(_cart_render_all(cookies)))


@mcp.tool()
def view_cart(checked_only: bool = False) -> str:
    """
    The whole AliExpress cart, as the web page shows it (read-only).

    Grouped into "Shipped by AliExpress" (Choice: one shipment, free above a
    threshold) and "Shipped by global sellers" (shipping per cart line). Each
    line: [x] when checked, quantity, variant, price, shipping, delivery
    window, store, cart_id. Then what checkout would charge for the checked
    lines, and a shipping analysis: every paid shipping line with the reason,
    the slowest line, and other channel carts (e.g. Combo Blast) that are not
    part of this cart.

    Args:
        checked_only: list only the checked lines.
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG
    try:
        c = _read_cart(cookies)
    except Exception as e:
        return f"Cart MTOP call failed: {e}"
    if not c["lines"]:
        return f"The cart is empty (server count: {c['header'].get('count')})."
    return cartlib.format_cart(c, show_unchecked=not checked_only)


@mcp.tool()
def select_cart_items(cart_ids: list[str], only: bool = False, selected: bool = True) -> str:
    """
    Check (or uncheck) cart lines for checkout, and return the checkout link
    for exactly these lines. This changes the real cart's selection, not its
    content.

    AliExpress remembers at most 10 checked lines (an 11th silently unchecks
    the oldest). The returned checkout link holds every given line, however
    many: it opens the order confirmation with exactly those lines, where the
    user decides to pay.

    Args:
        cart_ids: cart_id values from view_cart, most important first (when
            more than 10, the first 10 stay checked).
        only: uncheck every other line first, so the cart shows exactly these.
        selected: False unchecks the given lines instead.
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG
    try:
        report = _select(cookies, [str(c).strip() for c in cart_ids], only=only, selected=selected)
    except Exception as e:
        return f"Cart call failed: {e}"
    return report


def _select(cookies: dict[str, str], want: list[str], only: bool = False, selected: bool = True) -> str:
    before = _read_cart(cookies)
    titles = {ln["cart_id"]: f"{ln['title'][:40]} ({ln['variant']})" for ln in before["lines"]}
    state_of = {ln["cart_id"]: ln["checked"] for ln in before["lines"]}
    unknown = [c for c in want if c not in state_of]
    want = [c for c in want if c in state_of]
    out = []
    failed: dict[str, str] = {}
    if not selected:
        done, failed = _check_lines(cookies, [c for c in want if state_of[c]], on=False)
        out.append(f"Unchecked {len(done)} line(s).")
    else:
        if only:
            off = [c for c, on in state_of.items() if on and c not in want]
            done_off, f = _check_lines(cookies, off, on=False) if off else ([], {})
            failed.update(f)
            if done_off:
                out.append("Unchecked (not in the list): " + "; ".join(titles[c] for c in done_off))
        keep = want[:cartlib.MAX_CHECKED]
        on = [c for c in keep if not state_of[c]]
        done_on, f = _check_lines(cookies, on, on=True) if on else ([], {})
        failed.update(f)
        out.insert(0, f"Checked {len(done_on)} line(s); {len(keep) - len(on)} were already checked.")
        if len(want) > cartlib.MAX_CHECKED:
            out.append(f"AliExpress keeps only {cartlib.MAX_CHECKED} lines checked; the other "
                       f"{len(want) - cartlib.MAX_CHECKED} are in the checkout link below.")
    for c, why in failed.items():
        out.append(f"Failed {titles.get(c, c)}: {why}")
    if unknown:
        out.append("Not in cart: " + ", ".join(unknown))
    after = _read_cart(cookies)
    s = after["summary"]
    if s.get("total") is not None:
        out.append(f"Cart page now: {sum(1 for ln in after['lines'] if ln['checked'])} line(s) checked, "
                   f"goods {s.get('subtotal', 0):.2f} + shipping {s.get('shipping', 0):.2f} = "
                   f"{s['total']:.2f} {s.get('currency') or CURRENCY}")
    if selected and want:
        out.append(f"Checkout link ({len(want)} line(s)): {cartlib.checkout_url(want)}")
    return "\n".join(out)


# Lines this MCP put into the cart. Only these may be removed without asking:
# the user's own lines are never deleted by a plan.
MCP_LINES_FILE = Path(os.environ.get(
    "ALIEXPRESS_MCP_LINES", str(CREDENTIALS_PATH.parent / "aliexpress-mcp-lines.json")))


def _mcp_lines() -> dict[str, dict]:
    try:
        return json.loads(MCP_LINES_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _remember_lines(add: dict[str, dict] | None = None, drop: list[str] | None = None) -> None:
    lines = _mcp_lines()
    lines.update(add or {})
    for cid in drop or []:
        lines.pop(str(cid), None)
    MCP_LINES_FILE.write_text(json.dumps(lines, indent=1))


def _cart_add(cookies: dict[str, str], item_id: str, sku_id: str, quantity: int) -> str:
    """Add one variant; returns the new cart_id. Raises on refusal."""
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
    resp = mtop_call(
        "mtop.aliexpress.trade.cart.add", "1.0", payload,
        cookies=cookies, referer=f"{BASE_URL}/item/{item_id}.html", method="POST",
    )
    _cart_check(resp)
    data = resp.get("data") or {}
    if data.get("addFailed"):
        raise RuntimeError(data.get("errorMessage") or data.get("failReason") or "addFailed")
    cid = str(data.get("cartId"))
    _remember_lines({cid: {"item_id": str(item_id), "sku_id": str(sku_id), "added": time.strftime("%Y-%m-%d")}})
    return cid


def _check_lines(cookies: dict[str, str], cart_ids: list[str], on: bool = True):
    def edit(fields):
        fields["operationType"] = "checkbox"
        fields.setdefault("checkbox", {})["selected"] = on
    return _cart_line_action(cookies, cart_ids, edit)


@mcp.tool()
def add_to_cart(item_id: str, sku_id: str, quantity: int = 1) -> str:
    """
    Add a product variant to the AliExpress cart and check it for checkout.
    This changes the real cart (it does not buy anything). Adding a variant
    that is already in the cart raises that line's quantity instead.

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
    try:
        cid = _cart_add(cookies, item_id, sku_id, quantity)
    except Exception as e:
        return f"Add to cart failed: {e}"
    try:
        c = _read_cart(cookies)
        line = next((ln for ln in c["lines"] if ln["cart_id"] == cid), None)
        if line and not line["checked"]:
            _check_lines(cookies, [cid])
            c = _read_cart(cookies)
            line = next((ln for ln in c["lines"] if ln["cart_id"] == cid), None)
    except Exception as e:
        return f"Added {quantity} × item {item_id} (sku {sku_id}), cart_id {cid}, but checking it failed: {e}"
    if not line:
        return (f"Added item {item_id} (sku {sku_id}), cart_id {cid}, but it is not in the main cart — "
                "AliExpress may have put it in a channel cart (see view_cart).")
    ship = "free" if line["ship"] == 0 else (f"{line['ship']:.2f}" if line["ship"] is not None else "?")
    group = cartlib.GROUP_NAMES.get(line["group"], line["group"])
    return (
        f"Added and checked: {line['qty']} × {line['title'][:50]} ({line['variant']}), "
        f"{line['price']:.2f} each, shipping {ship}, delivery {line['delivery'][0]}…{line['delivery'][1]}, "
        f"{group}. cart_id {cid}."
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

    def edit(fields):
        fields["operationType"] = "update_quantity"
        fields["quantityView"] = {"current": int(quantity)}

    try:
        done, failed = _cart_line_action(cookies, [cart_id], edit)
    except Exception as e:
        return f"Cart call failed: {e}"
    if failed:
        return f"cart_id {cart_id}: {next(iter(failed.values()))}"
    return f"cart_id {cart_id}: quantity now {quantity}."


@mcp.tool()
def remove_from_cart(cart_ids: list[str]) -> str:
    """
    Remove items from the AliExpress cart. This changes the real cart.

    Args:
        cart_ids: cart_id values from view_cart (not item_ids). Confirm the exact
            list with the user before calling, unless they are lines this MCP
            added itself that a plan replaced.
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG

    def edit(fields):
        fields["operationType"] = "delete"

    try:
        done, failed = _cart_line_action(cookies, cart_ids, edit)
    except Exception as e:
        return f"Cart call failed: {e}"
    _remember_lines(drop=done)
    lines = [f"Removed {len(done)} item(s)."]
    if failed:
        lines.append("Failed: " + "; ".join(f"{c}: {m}" for c, m in failed.items()))
    return "\n".join(lines)


# ─── Main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
