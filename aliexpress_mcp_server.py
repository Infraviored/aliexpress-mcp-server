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
import asyncio
import reportserver
import uuid
import functools
import threading
import time
import uuid
import hashlib
import logging
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP, Image

import browser_actions
import cart as cartlib
import checkout as checkoutlib
import report
import deals
import orders as orderslib
import private

# ─── Configuration ──────────────────────────────────────────────────────────

CREDENTIALS_PATH = Path(
    os.environ.get("ALIEXPRESS_CREDENTIALS", "~/.mcp-credentials/aliexpress.json")
).expanduser()

COUNTRY = os.environ.get("ALIEXPRESS_COUNTRY", "DE")
CURRENCY = os.environ.get("ALIEXPRESS_CURRENCY", "EUR")
report.CURRENCY = CURRENCY
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


def _save_cookies(update: dict[str, str]) -> None:
    """Merge `update` into the credential file (e.g. a refreshed _m_h5_tk)."""
    try:
        data = json.loads(CREDENTIALS_PATH.read_text())
    except (OSError, ValueError):
        return
    data["cookies"] = {**data.get("cookies", {}), **update}
    try:
        private.write_json(CREDENTIALS_PATH, data)
    except OSError as e:
        logger.warning("could not save refreshed cookies: %s", e)


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
    if captcha and (left := _bot_cooldown_left()) > 0:
        raise RuntimeError(f"bot check active, paused for another {int(left)} s")
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
            s in ret_str for s in ("TOKEN_EMPTY", "TOKEN_EXPIRED", "TOKEN_EXOIRED")
        ):
            fresh = {name: resp.cookies.get(name) for name in resp.cookies if resp.cookies.get(name) is not None}
            if fresh:
                # Keep the new token — in the caller's dict for the rest of this
                # tool call, on disk for later ones — or every call fails once.
                cookies.update(fresh)
                _save_cookies(fresh)
            return mtop_call(api, version, payload, cookies=cookies, retries=retries - 1, referer=referer, method=method, captcha=captcha)

        # Bot check: solve the slider (or click the reCAPTCHA box) in real Chrome
        # on a virtual display, retry once.
        punish_url = (data.get("data") or {}).get("url") if isinstance(data.get("data"), dict) else None
        if "FAIL_SYS_USER_VALIDATE" in ret_str:
            _bot_note(True)
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
                _bot_note(False)
                # Passing the check sets a new token: allow one token refresh, no second captcha.
                return mtop_call(api, version, payload, cookies=load_cookies(), retries=1, referer=referer, method=method, captcha=False)
        elif ret_str.startswith("SUCCESS") and BOT_CHECK:
            _bot_note(False)

        return data


# Set while AliExpress answers with a bot check we could not pass.
BOT_CHECK: dict[str, float] = {}
MTOP_MIN_GAP = float(os.environ.get("ALIEXPRESS_MTOP_GAP", "2.0"))
MTOP_JITTER = float(os.environ.get("ALIEXPRESS_MTOP_JITTER", "1.5"))
# At most this many AliExpress calls per minute over all processes (25 per minute in a row ended in a bot check).
MTOP_PER_MINUTE = int(os.environ.get("ALIEXPRESS_MTOP_PER_MINUTE", "12"))
# A quick tool call (not a background job) prices at most this many uncached product pages; the rest waits for the next call.
FRESH_PAGES_QUICK = int(os.environ.get("ALIEXPRESS_FRESH_PAGES", "4"))
BOT_COOLDOWN = float(os.environ.get("ALIEXPRESS_BOT_COOLDOWN", "600"))
_PACE_FILE = CREDENTIALS_PATH.parent / "aliexpress-mtop-last"
_BOT_FILE = CREDENTIALS_PATH.parent / "aliexpress-botcheck.json"


_CALL = threading.local()      # per tool call: AliExpress calls, seconds spent pacing, background flag
_FOREGROUND = 0                # quick tool calls running now (they go before background jobs)
_FG_LOCK = threading.Lock()


CALL_LOG = CREDENTIALS_PATH.parent / "aliexpress-calls.log"


def _log_call(kind: str) -> None:
    """One line per AliExpress request (time, process, thread, kind): what came before a bot check."""
    import fcntl
    try:
        CALL_LOG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(CALL_LOG, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)           # rotation and append are one step across processes and threads
            if os.fstat(f.fileno()).st_size > 1_000_000:
                CALL_LOG.replace(CALL_LOG.with_suffix(".log.1"))
                f.close()
                f = open(CALL_LOG, "a")
                fcntl.flock(f, fcntl.LOCK_EX)
            f.write(f"{time.strftime('%H:%M:%S')} pid={os.getpid()} {threading.current_thread().name} {kind}\n")
            f.flush()
            f.close()
    except OSError:
        pass


def _mtop_pace(kind: str = "mtop") -> None:
    """Keep a gap (plus jitter) between MTOP calls, across all server processes:
    bursts are what trigger the bot check. Background jobs let quick tool calls go first."""
    import fcntl
    import random
    t0 = time.time()
    _log_call(kind)
    _CALL.calls = getattr(_CALL, "calls", 0) + 1
    give_up = time.time() + 6.0      # quick calls go first, but never starve a background job
    while getattr(_CALL, "background", False) and _FOREGROUND > 0 and time.time() < give_up:
        time.sleep(0.2)
    try:
        _mtop_pace_locked(fcntl, random)
    finally:
        _CALL.waited = getattr(_CALL, "waited", 0.0) + time.time() - t0


def _mtop_pace_locked(fcntl, random) -> None:
    _PACE_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(_PACE_FILE, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        try:
            times = [float(x) for x in json.loads(f.read() or "[]")]
        except (ValueError, TypeError):
            times = []                                      # empty, damaged or the old single-number format
        now = time.time()
        times = [x for x in times if now - 60 < x <= now]
        wait = (times[-1] + MTOP_MIN_GAP + random.uniform(0, MTOP_JITTER) - now) if times else 0.0
        if MTOP_PER_MINUTE > 0 and len(times) >= MTOP_PER_MINUTE:
            wait = max(wait, times[-MTOP_PER_MINUTE] + 60 - now)     # the window is full: wait until its oldest call is a minute old
        if wait > 0:
            time.sleep(wait)
        times.append(time.time())
        f.seek(0)
        f.truncate()
        f.write(json.dumps(times[-max(1, MTOP_PER_MINUTE):]))


def _bot_note(blocked: bool) -> None:
    """Remember across processes that AliExpress asked for a bot check (or that it passed)."""
    if blocked:
        _log_call("BOT-CHECK")
        BOT_CHECK["since"] = BOT_CHECK.get("since") or time.time()
        BOT_CHECK["last"] = time.time()
        private.write_json(_BOT_FILE, {"since": BOT_CHECK["since"], "last": BOT_CHECK["last"]})
    else:
        BOT_CHECK.clear()
        _BOT_FILE.unlink(missing_ok=True)


def _bot_cooldown_left() -> float:
    """Seconds still to wait after the last bot check: asking again only deepens the block.
    The file is the truth (other processes write and clear it), so it is read every time."""
    try:
        BOT_CHECK.update(json.loads(_BOT_FILE.read_text()))
    except (OSError, ValueError):
        BOT_CHECK.clear()
        return 0.0
    return max(0.0, BOT_CHECK.get("last", 0) + BOT_COOLDOWN - time.time())


REPORT_HINT = ("\n[Not finished: Florian sees no pictures in chat. Pass the finalists to make_report "
               "(page with pictures, links, prices, variants) and send that page, not this list.]")


def blocked_msg() -> str:
    """Why a lookup came back empty, when it was the bot check."""
    if _bot_cooldown_left() <= 0:
        return ""
    left = max(1, round(_bot_cooldown_left() / 60))
    return (f" AliExpress bot check (captcha) active: this is NOT an expired session and the product is not gone."
            f" Calls are paused for about {left} more min. Fastest way out: .venv/bin/python import_profile.py --ask"
            " (the user passes the check in the remote browser card, the cookies are imported).")


# ─── Parsing Helpers ────────────────────────────────────────────────────────

PRICE_RE = re.compile(
    r"(?:C\$|CA\$|US\$|\$|€|CAD|USD|EUR)\s*"
    r"([\d.,]+)"
    r"|([\d.,]+)\s*(?:€|EUR)",
)
ITEM_ID_RE = re.compile(r"/item/(\d+)\.html")


def parse_price(text: str) -> Optional[float]:
    """Pull the first price-looking number out of a string."""
    if not text:
        return None
    m = PRICE_RE.search(text)
    if m:
        return cartlib.money(m.group(1) or m.group(2))
    # Fallback: bare number (dot-decimal)
    m = re.search(r"([\d,]+\.\d{2})", text)
    if m:
        return cartlib.money(m.group(1))
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


# Items seen as "Kombi-Knaller" in a search: the planner prices them without import
# charge when 3+ pieces of such items go into the combo cart together.
COMBO_ITEMS: set[str] = set()
COMBO_MIN_PIECES = 3
# Lines the combo checkout takes per order ("Limit exceeded, remove N items"); 0 = not measured.
COMBO_MAX_LINES = int(os.environ.get("ALIEXPRESS_COMBO_MAX_LINES", "0"))


def _parse_search_json(html: str) -> list[dict]:
    """
    The whole result page (60 hits) from the page's embedded `itemList`
    JSON. Only the first ~9 are also rendered as HTML cards.

    Choice shows as the selling point `platformFreeShipping_atm` ("free
    shipping over 10,00€"); orders and ship-from sit in trace.pdpParams.pdp_cdi.
    """
    from urllib.parse import unquote

    i = html.find('"itemList":{"content":[')
    if i < 0:
        return []
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, i + len('"itemList":'))
    except ValueError:
        return []
    out = []
    for it in obj.get("content") or []:
        pid = str(it.get("productId") or it.get("redirectedId") or "")
        title = ((it.get("title") or {}).get("displayTitle") or "").strip()
        if not pid or not title:
            continue
        prices = it.get("prices") or {}
        sale = (prices.get("salePrice") or {}).get("minPrice")
        orig = (prices.get("originalPrice") or {}).get("minPrice")
        points = it.get("sellingPoints") or []
        texts = [re.sub(r"<[^>]+>", "", m.get("value") or "") for sp in points
                 for m in sp.get("materials") or [] if m.get("type") == "TEXT"]
        sources = {str(sp.get("source") or "") for sp in points}
        try:
            cdi = json.loads(unquote(((it.get("trace") or {}).get("pdpParams") or {}).get("pdp_cdi") or "{}"))
        except ValueError:
            cdi = {}
        try:
            orders = int(cdi.get("order")) if cdi.get("order") else None
        except (TypeError, ValueError):
            orders = None
        out.append({
            "item_id": pid,
            "title": title[:200],
            "price": sale,
            "original_price": orig if orig and sale and orig > sale else None,
            "currency": (prices.get("salePrice") or {}).get("currencyCode"),
            "discount_pct": round(100 * (1 - sale / orig)) if sale and orig and orig > sale else None,
            "rating": (it.get("evaluation") or {}).get("starRating"),
            "orders": orders,
            "sold_count": (it.get("trade") or {}).get("tradeDesc"),
            "shipping": next((t for t in texts if re.search(r"versand|shipping|delivery", t, re.I)), None),
            "choice": any("platformFreeShipping" in x or "choice" in x.lower() for x in sources),
            "ship_from": cdi.get("shipFrom"),
            # "Kombi-Knaller" / Bundle Deals: 3+ such items in the combo cart, no import charge
            "combo": (it.get("rainbow") or {}).get("rainbowType") == "channelProduct" or "npieces" in sources,
            "url": f"{BASE_URL}/item/{pid}.html",
        })
        if out[-1]["combo"]:
            COMBO_ITEMS.add(str(pid))
    return out


def parse_search_results(html: str) -> list[dict]:
    """Product hits of an AliExpress search results page."""
    return _parse_search_json(html) or _parse_search_cards(html)


# ─── MCP Server ─────────────────────────────────────────────────────────────

mcp = FastMCP("aliexpress", dependencies=["httpx", "beautifulsoup4"])


def tool(slow: bool = False):
    """Register a tool that runs in its own thread, so a slow call never holds up a quick one
    (FastMCP runs plain functions inside its event loop), and ends its text with how long it took.
    `slow` tools are served after quick ones when both wait for the pacing gap."""
    def deco(fn):
        @functools.wraps(fn)
        async def run(*args, **kwargs):
            def call():
                global _FOREGROUND
                _CALL.calls, _CALL.waited, _CALL.background = 0, 0.0, slow
                if not slow:
                    with _FG_LOCK:
                        _FOREGROUND += 1
                t0 = time.time()
                try:
                    out = fn(*args, **kwargs)
                finally:
                    if not slow:
                        with _FG_LOCK:
                            _FOREGROUND -= 1
                took = time.time() - t0
                if isinstance(out, str) and took >= 1.0 and "AliExpress call(s)" not in out[-300:]:
                    out += (f"\n\n(took {took:.0f} s: {_CALL.calls} AliExpress call(s), {_CALL.waited:.0f} s of it waiting "
                            "for the pacing gap between calls)")
                return out
            return await asyncio.to_thread(call)
        mcp.add_tool(run, name=fn.__name__, description=fn.__doc__)
        return fn
    return deco


@tool()
def search_products(
    query: str,
    min_rating: float = 0,
    max_price: float = 0,
    sort_by: str = "best_match",
    page: int = 1,
    with_shipping: bool = False,
    choice_only: bool = False,
    pieces: int = 1,
    match: str = "",
    avoid: str = "",
    rank_top: int = 12,
    cart_id: str = "",
    combo_only: bool = False,
) -> str:
    """
    Search AliExpress for products (60 results per page; the first 25 are listed).

    The price shown is the CHEAPEST variant, which is often bait: an
    accessory, a bare chip or 1 piece of a pack (ESP32-S3 "6.19 €" was a
    breakout board; the dev board was 12.19 €). Judge with best_offer.
    THE POINT OF THIS SERVER IS THE CHEAPEST DEAL: any saving is a reason to
    switch, also 0.10 EUR. There is no "not worth it" threshold; only bait
    variants and wrong articles (check get_variant_images) rule an offer out.
    To compare with a line already in the cart pass `cart_id`; to recheck the
    whole cart use recheck_cart.
    NEED MORE THAN ONE PIECE? Pass `pieces`: the search then prices the top
    hits for exactly that many pieces (pack sizes, shipping, import charge,
    store discounts) and ranks them by what the order really costs, not by the single
    piece price a pack ladder hides ("10PCS" in another item can be cheaper
    per piece AND in total).
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
        pieces: How many pieces you need. Above 1 the top hits are priced
            for that quantity and ranked by net total (see above).
        match: With pieces: words every variant (or the title) must contain,
            e.g. "GY-302" or "N16R8".
        avoid: With pieces: words that rule a hit or variant out.
        rank_top: With pieces: how many hits to price (product-page calls,
            cached for hours; the rest are not looked at).
        cart_id: A line already in the cart for this need: it is priced the
            same way and the result ends with "CHEAPER THAN CART LINE by X"
            or "already the cheapest". pieces is taken from that line
            (quantity x pack size); match/avoid default to the line's list tags.
        combo_only: Only "Kombi-Knaller" (Bundle Deals) hits: 3 or more such
            items in the combo cart (a separate channel cart, see view_cart)
            are ordered without import charge. This IS the search inside the
            Kombi-Knaller; every hit is flagged "Kombi" anyway.
    """
    try:
        products = _search(query, sort_by, page, choice_only)
    except PermissionError:
        return AUTH_EXPIRED_MSG
    except RuntimeError as e:
        return f"Search not done: {e}" + blocked_msg()
    if combo_only:
        products = [p for p in products if p.get("combo")]
    if min_rating > 0:
        products = [p for p in products if p.get("rating") and p["rating"] >= min_rating]
    if max_price > 0:
        products = [p for p in products if p.get("price") is not None and p["price"] <= max_price]
    if (pieces > 1 or cart_id) and products:
        line = None
        if cart_id:
            cookies = load_cookies()
            if not cookies:
                return AUTH_EXPIRED_MSG
            try:
                line = next((x for x in _read_cart(cookies)["lines"] if x["cart_id"] == str(cart_id)), None)
            except Exception as e:
                return f"Cart MTOP call failed: {e}"
            if not line:
                return f"cart_id {cart_id} is not in the cart (view_cart lists the ids)."
            tag = _line_store().get(line["cart_id"], {})
            match, avoid = match or tag.get("match", ""), avoid or tag.get("avoid", "")
            # the line is compared like for like: its own quantity x pack, whatever `pieces` says
            pieces = line["qty"] * deals.pack_size(line["variant"])
        return _rank_for_pieces(query, products, max(1, pieces), match, avoid, rank_top, choice_only, line)

    if not products:
        filters = []
        if min_rating > 0:
            filters.append(f"rating ≥ {min_rating}")
        if max_price > 0:
            filters.append(f"price ≤ {max_price:.2f}")
        suffix = f" with {', '.join(filters)}" if filters else ""
        return f"No products found for '{query}'{suffix}." + blocked_msg()

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
        if p.get("combo"):
            line += " · Kombi (3+ in the combo cart: no import charge)"
        if p.get("shipping"):
            line += f"\n  shipping: {p['shipping']}"
        line += f"\n  item_id: {p['item_id']}"
        lines.append(line)
    lines.append(REPORT_HINT)
    return "\n".join(lines)


def _price_hits(products: list[dict], pieces: int, match: str, avoid: str, rank_top: int,
                choice_only: bool) -> tuple[list[tuple[dict, dict]], int, list[str]]:
    """Top hits priced for `pieces`: ([(best way, hit)] cheapest net first, hits looked at, notes)."""
    avoid_w, match_w = _words(avoid), _words(match)
    hits = [h for h in products if not any(w in (h.get("title") or "").lower() for w in avoid_w)]
    named = [h for h in hits if all(w in (h.get("title") or "").lower() for w in match_w)]
    hits = named or hits
    # Search order (its price is only a hint), Choice first: shared shipping, about a week to DE.
    hits = sorted(hits, key=lambda h: not h.get("choice"))[: max(1, min(int(rank_top), 25))]
    spec = {"pieces": pieces, "match": match, "avoid": avoid, "seller": "choice" if choice_only else "any"}
    rows, notes = [], []
    fresh = 0
    for h in hits:
        cached = h["item_id"] in _PDP_CACHE
        if not cached and not getattr(_CALL, "background", False) and FRESH_PAGES_QUICK > 0:
            if fresh >= FRESH_PAGES_QUICK:
                notes.append(f"{h['item_id']}: not priced (call limit per request, to avoid the bot check; ask again for more)")
                continue
            fresh += 1
        try:
            ways, n = _ways_for(h["item_id"], spec, 0.0)
        except PermissionError:
            raise
        except Exception as e:
            logger.debug("ranking %s failed: %s", h["item_id"], e)
            continue
        if not cached:
            time.sleep(0.5)
        notes += n[:1] if not ways else []
        if ways:
            rows.append((ways[0], h))
    rows.sort(key=lambda r: (r[0]["net"], -r[0]["pieces"]))
    return rows, len(hits), notes


def _current_way(ln: dict, pieces: int) -> Optional[dict]:
    """What the cart line costs now, priced like the hits (same shipping, import, discounts)."""
    ways, _ = _ways_for(ln["item_id"], {"pieces": pieces, "sku_id": ln["sku_id"], "qty": ln["qty"]}, 0.0)
    return ways[0] if ways else None


def _saving(rows: list[tuple[dict, dict]], cur: Optional[dict]) -> tuple[float, Optional[tuple[dict, dict]]]:
    """(saving in net EUR, the cheaper row) of the best hit against the cart line; 0 and None if the line wins."""
    rows = [r for r in rows if cur and r[0]["pieces"] >= cur["pieces"]]   # fewer pieces is no saving
    if not rows or not cur:
        return 0.0, None
    gain = cur["net"] - rows[0][0]["net"]
    return (gain, rows[0]) if gain > 0.004 else (0.0, None)


def _way_text(w: dict) -> str:
    line = deals.fmt_way(w, CURRENCY)
    if w["save"]:
        line += f" - {w['save']:.2f} ({w['save_how']})"
    return line + f" = {w['net']:.2f} net ({w['net'] / max(1, w['pieces']):.2f}/pc)" + (f" - ~{w['days']} days" if w.get("days") else "")


def _rank_for_pieces(query: str, products: list[dict], pieces: int, match: str, avoid: str,
                     rank_top: int, choice_only: bool, line: Optional[dict] = None) -> str:
    """The top search hits priced for `pieces` (best pack mix, shipping, import, discounts), cheapest net first.
    With `line` (a cart line) it ends with the verdict against that line."""
    try:
        rows, looked, notes = _price_hits(products, pieces, match, avoid, rank_top, choice_only)
    except PermissionError:
        return AUTH_EXPIRED_MSG
    if not rows:
        return (f"No hit of '{query}' could be priced for {pieces} pieces." + blocked_msg()
                + ("".join(f"\n{n}" for n in notes[:5])))
    out = [f"{len(rows)} of the top {looked} hits for '{query}' priced for {pieces} pieces "
           f"(net incl. shipping, {CURRENCY} {DUTY_PER_LINE:.2f} import per line, discounts; ranked by what the order costs, at least {pieces} pcs):"]
    for i, (w, h) in enumerate(rows, 1):
        head = f"{i}. {h['title'][:80]}" + (" - Choice" if w["offer"]["choice"] else "")
        if h.get("combo"):
            head += f" - KOMBI (with 3+ combo items {w['net'] - (w.get('duty') or 0):.2f} net, no import charge)"
        if h.get("orders") is not None:
            head += f" - {h['orders']} orders"
        if h.get("rating"):
            head += f" *{h['rating']}"
        out.append(head + f"\n   {_way_text(w)}\n   item_id: {h['item_id']}")
    out.append("Variants priced by name only: check get_variant_images on the pick; bait variants are skipped when far cheaper than the rest.")
    out += notes[:5]
    if line:
        try:
            cur = _current_way(line, pieces)
        except PermissionError:
            return AUTH_EXPIRED_MSG
        except Exception as e:
            logger.debug("cart line %s not priced: %s", line["cart_id"], e)
            cur = None
        gain, best = _saving(rows, cur)
        if not cur:
            out.append(f"CART LINE {line['cart_id']}: could not be priced, compare by hand.")
        elif best:
            w, h = best
            out.append(f"CHEAPER THAN CART LINE {line['cart_id']} by {gain:.2f} {CURRENCY} net "
                       f"(line now {cur['net']:.2f}, #1 {w['net']:.2f}, item {h['item_id']}). Any saving is a reason to switch: "
                       "there is no trivial-saving threshold. Only a bait variant or the wrong article rules it out (get_variant_images).")
        else:
            out.append(f"CART LINE {line['cart_id']} is already the cheapest of the {len(rows)} priced ({cur['net']:.2f} net).")
    return "\n".join(out)


SEARCH_TTL = float(os.environ.get("ALIEXPRESS_SEARCH_TTL", "1800"))
_SEARCH_CACHE: dict[tuple, tuple[float, list[dict]]] = {}


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
    key = (query.strip().lower(), sort_by, page, choice_only)
    hit = _SEARCH_CACHE.get(key)
    if hit and time.time() - hit[0] < SEARCH_TTL:
        return [dict(h) for h in hit[1]]
    if (left := _bot_cooldown_left()) > 0:        # asking again during a block only deepens it
        raise RuntimeError(f"bot check active, paused for another {int(left)} s")
    _mtop_pace("search")                                 # a search page is as much a request as an mtop call
    client = get_client()
    try:
        resp = client.get(url_path, params=params)
        if check_auth_redirect(resp):
            raise PermissionError("auth expired")
        found = parse_search_results(resp.text)
        if "punish" in str(resp.url).lower() or "_____tmd_____" in str(resp.url) or (
                not found and re.search(r"x5secdata|_____tmd_____|punish", resp.text[:20000])):
            _bot_note(True)
            raise RuntimeError("bot check: AliExpress asked for a slider on the search page; "
                               f"calls pause for {int(BOT_COOLDOWN)} s")
        resp.raise_for_status()
        # The filter lets the odd hit through whose card lacks the Choice
        # badge; the product page decides (shop_list checks it there).
        if found:
            _SEARCH_CACHE[key] = (time.time(), found)
        return [dict(h) for h in found]
    finally:
        client.close()


# Product pages repeat within one deal search (variants, pictures, best_offer).
PDP_TTL = 600
_PDP_CACHE: dict[str, tuple[float, dict]] = {}
# Also on disk, so a new server process (or a direct script run) does not fetch the same
# product pages again: fewer calls, fewer bot checks. Prices change slowly enough for hours.
PDP_DISK_TTL = float(os.environ.get("ALIEXPRESS_PDP_DISK_TTL", "10800"))
PDP_DISK_MAX = 40
_PDP_FILE = CREDENTIALS_PATH.parent / "aliexpress-pdp-cache.json"


def _pdp_disk_load() -> dict:
    try:
        return json.loads(gzip.decompress(_PDP_FILE.read_bytes()))
    except (OSError, ValueError, EOFError):
        return {}


_PDP_WRITE_LOCK = threading.Lock()


def _pdp_disk_store(item_id: str, resp: dict) -> None:
    import tempfile
    with _PDP_WRITE_LOCK:  # threads of this process; other processes only ever replace whole files
        data = {k: v for k, v in _pdp_disk_load().items() if time.time() - v["at"] < PDP_DISK_TTL}
        data[item_id] = {"at": time.time(), "resp": resp}
        for k in sorted(data, key=lambda k: data[k]["at"])[:-PDP_DISK_MAX]:
            del data[k]
        tmp = None
        try:
            _PDP_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=_PDP_FILE.parent, prefix=_PDP_FILE.name + ".", suffix=".tmp")  # mode 600
            with os.fdopen(fd, "wb") as f:
                f.write(gzip.compress(json.dumps(data).encode()))
            os.replace(tmp, _PDP_FILE)
        except OSError:
            if tmp:
                Path(tmp).unlink(missing_ok=True)


def _fetch_pdp_mtop(item_id: str) -> Optional[dict]:
    """
    Call the PDP MTOP endpoint and return the raw response dict, or None on failure.
    Tries the PC endpoint first, then the msite endpoint as a fallback.
    """
    hit = _PDP_CACHE.get(item_id)
    if hit and time.time() - hit[0] < PDP_TTL:
        return hit[1]
    disk = _pdp_disk_load().get(item_id)
    if disk and time.time() - disk["at"] < PDP_DISK_TTL:
        _PDP_CACHE[item_id] = (time.time(), disk["resp"])
        return disk["resp"]
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
            _pdp_disk_store(item_id, resp)
            return resp
        # Some endpoints return data even without SUCCESS::API_SUCCESS in ret
        if resp.get("data") and isinstance(resp["data"], dict) and resp["data"]:
            return resp
        logger.debug("MTOP %s ret=%s", api, ret)
    return None


def _ship_biz(result: dict, layouts_first: str = "originalLayoutResultList") -> dict:
    """The shipping offer of a product response: bizData of the first layout, `layouts_first` preferred."""
    ship = result.get("SHIPPING") if isinstance(result.get("SHIPPING"), dict) else {}
    other = "deliveryLayoutInfo" if layouts_first == "originalLayoutResultList" else "originalLayoutResultList"
    layouts = ship.get(layouts_first) or ship.get(other) or []
    biz = layouts[0].get("bizData") if layouts and isinstance(layouts[0], dict) else None
    return biz if isinstance(biz, dict) else {}


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
    biz = _ship_biz(result)
    if biz:
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


@tool()
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

    if not d or not (d.get("title") or d.get("price")):
        return (
            f"Could not extract product data for item {item_id}. "
            "The MTOP API returned no data — session may be expired or the item "
            "may be region-restricted. Try re-saving credentials." + blocked_msg()
        )

    lines = [f"# {d.get('title') or item_id}"]
    lines.append(f"URL: {d['url']}")
    if d.get("price") is not None:
        if d.get("price_range"):
            lo, hi = d["price_range"]
            line = f"Price: {lo:.2f}–{hi:.2f} {CURRENCY}"
        else:
            line = f"Price: {d['price']:.2f} {CURRENCY}"
        if d.get("original_price") and d["original_price"] > d["price"]:
            line += f" (was {d['original_price']:.2f}"
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
        ship_line = "Shipping: " + ("Free" if d["shipping_cost"] == 0 else f"{d['shipping_cost']:.2f} {CURRENCY}")
        lines.append(ship_line)
    if d.get("shipping_estimate"):
        eta_line = f"Estimated delivery: {d['shipping_estimate']}"
        if d.get("ship_days_min") and d.get("ship_days_max"):
            eta_line += f" ({d['ship_days_min']}–{d['ship_days_max']} days)"
        lines.append(eta_line)
    if d.get("ship_from"):
        lines.append(f"Ships from: {d['ship_from']}")
    return "\n".join(lines)


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
        pack = deals.pack_size(label)
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
    b = _ship_biz(res, "deliveryLayoutInfo")
    if b:
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
    imgs = (res.get("HEADER_IMAGE_PC") or {}).get("imagePathList") or []
    return {
        "item_id": item_id,
        "title": ((res.get("PRODUCT_TITLE") or {}).get("text")) or item_id,
        "url": f"{BASE_URL}/item/{item_id}.html",
        "image": imgs[0] if imgs and isinstance(imgs[0], str) else None,
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


COUPON_MEMORY_DAYS = 14


def _coupon_stamps() -> dict[str, float]:
    """
    Public code coupons this MCP collected within COUPON_MEMORY_DAYS, with
    when. An order may have used one up meanwhile; forgetting it after a
    while means it is shown as collectable again rather than counted forever.
    """
    try:
        raw = json.loads(COUPONS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    if isinstance(raw, list):  # older format: ids without dates, too old to trust
        return {}
    cutoff = time.time() - COUPON_MEMORY_DAYS * 86400
    return {cid: ts for cid, ts in raw.items() if ts >= cutoff}


def _collected_coupons() -> set[str]:
    return set(_coupon_stamps())


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


def _words(text: str) -> list[str]:
    return [w.lower() for w in (text or "").split()]


def _by_match(variants: list[dict], match: str) -> list[dict]:
    """Variants whose name contains every word of `match` (all when empty)."""
    words = _words(match)
    return [v for v in variants if all(w in v["label"].lower() for w in words)]


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


@tool()
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
        return (f"No variant data for {item_id}." + blocked_msg()) if _bot_cooldown_left() > 0 else \
            f"No variant data for {item_id} (session expired or product gone)."
    total = len(offer["variants"])
    rows = _by_match(offer["variants"], match)
    rows.sort(key=lambda r: (not r["salable"], r["unit"] if r["unit"] is not None else 1e9))

    head = f"{len(rows)} of {total} variant(s)" + (f" matching '{match}'" if match.strip() else "")
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
    lines.append(REPORT_HINT)
    return "\n".join(lines)


def _fetch_image(url: str) -> Optional[bytes]:
    try:
        r = httpx.get(url + "_220x220.jpg", timeout=15.0, headers={"User-Agent": USER_AGENT})
        return r.content if r.status_code == 200 else None
    except Exception:
        return None


@tool()
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
    rows = _by_match(offer["variants"], match)
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


@tool()
def best_offer(item_id: str, pieces: int, match: str = "", choice_order_over_threshold: bool = True,
               packs: str = "") -> str:
    """
    Cheapest way to get at least `pieces` pieces from one product (any saving
    over what you have is a reason to switch, no trivial-saving threshold): compares
    pack sizes and mixes (10×1, 2×5, 1×10 …) including shipping and the
    store's discounts/coupons and the import charge per cart line (ranked by
    net price; lists every discount). Non-Choice shipping is measured as
    A + B per extra unit; Choice shipping counts as free when the Choice part
    of the order is over the threshold.

    Args:
        item_id: AliExpress item ID.
        pieces: How many pieces you need.
        match: Restrict to variants whose name contains all these words
            (e.g. "5V" or "N16R8 soldered").
        choice_order_over_threshold: For Choice items, whether the whole order's
            Choice part already passes the free-shipping threshold.
        packs: Pieces per variant when its name does not say so ("5PCS" is read
            automatically, "5 Black" is not). Format "words=pieces; words=pieces",
            e.g. "5 black=5; 1 black=1" for filament where "5 Black" is a bundle
            of 5 spools. Without it every such variant counts as ONE piece.
    """
    offer = _pdp_offer(item_id)
    if not offer:
        return f"No variant data for {item_id}." + blocked_msg()
    vs = [dict(v) for v in _by_match(offer["variants"], match) if v["salable"]]
    if not vs:
        return f"No available variant matches '{match}'."
    for rule in filter(None, (r.strip() for r in packs.split(";"))):
        words, _, n = rule.rpartition("=")
        if not n.strip().isdigit() or int(n) < 1 or not words.strip():
            return f"Bad packs rule '{rule}': use 'words=pieces; words=pieces' with pieces >= 1."
        for v in _by_match(vs, words):
            v["pack"] = int(n)
            v["unit"] = v["price"] / v["pack"] if v["price"] else None
            # "5 Black" and "1 Black" are rungs of one ladder: group them without the pack number
            v["group"] = re.sub(r"\s+", " ", re.sub(rf"(?<![\w.]){int(n)}(?![\w.])", "", v["label"], count=1)).strip(" /-").lower()
    quote = _freight(offer)
    ways = deals.best_ways(vs, max(1, pieces), quote, offer["choice"] and choice_order_over_threshold,
                           limit=50, duty=DUTY_PER_LINE)
    disc = _discounts(item_id, vs[0]["sku_id"])
    for w in ways:
        w["save"], w["save_how"] = deals.discount_for(disc, w["goods"])
        w["net"] = w["total"] - w["save"]
    ways.sort(key=lambda w: (w["net"], -w["pieces"]))
    lines = [f"{offer['title'][:90]} ({item_id})",
             ("Choice" if offer["choice"] else deals.fmt_quote(quote, CURRENCY))]
    if pieces > 1 and not packs and all(v["pack"] == 1 for v in vs):
        lines.append("WARNING: no pack size in any variant name, so each variant is counted as ONE piece. "
                     "If a variant is a multi-piece bundle, check the product page and pass packs=\"name=pieces\".")
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


@tool()
def get_discounts(item_id: str, subtotal: float = 0) -> str:
    """
    The product's "Discounts & coupon codes": automatic store discount tiers,
    store coupons (collect / follow / code), coins. With `subtotal` (your
    order total in this store) it says what applies and what the next tier
    would give. best_offer and shop_list already apply these.

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
        with _STATE_LOCK:
            private.write_json(COUPONS_FILE, {**_coupon_stamps(), **{cid: time.time() for cid in got}})
    _DISC_CACHE.pop(item_id, None)
    return res


@tool()
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


@tool()
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
# Import charges per cart line (EU flat duty + VAT, measured at checkout for
# DE). Every extra line costs this, whatever its price — the biggest reason a
# list of cheap parts gets expensive.
DUTY_PER_LINE = float(os.environ.get("ALIEXPRESS_DUTY_PER_LINE", str(checkoutlib.DUTY_PER_LINE)))


def _choice_only(spec: dict) -> bool:
    return str(spec.get("seller", "any")).lower() == "choice"


def _candidate_ids(spec: dict) -> list[str]:
    """Given item_ids plus the top search hits for `query` (both compete)."""
    ids = [str(x) for x in spec.get("item_ids") or []]
    if not spec.get("query"):
        return ids
    avoid, match = _words(spec.get("avoid", "")), _words(spec.get("match", ""))
    hits = [h for h in _search(spec["query"], spec.get("sort_by", "best_match"), choice_only=_choice_only(spec))
            if not any(w in (h.get("title") or "").lower() for w in avoid)]
    if _choice_only(spec):
        hits = [h for h in hits if h.get("choice")] or hits
    # 60 hits per page: keep those naming the match words, cheapest first.
    # The search price is only a hint (often a bait variant); the product
    # page decides.
    named = [h for h in hits if all(w in (h.get("title") or "").lower() for w in match)]
    hits = sorted(named or hits, key=lambda h: h.get("price") if h.get("price") is not None else 1e9)
    # More than one piece: the cheapest single piece is no guide (a "10PCS" ladder in a
    # dearer-looking item wins), so look at more items.
    default = 10 if int(spec.get("pieces", 1) or 1) > 1 else 5
    for h in hits[: int(spec.get("candidates", default))]:
        if h["item_id"] not in ids:
            ids.append(h["item_id"])
    return ids


def _combo_free(w: dict, on: bool) -> None:
    """Take the import charge off a Kombi way (3+ combo pieces in the combo order), or put it back."""
    if not w.get("combo") or bool(w.get("combo_free")) == on:
        return
    d = -w["duty_back"] if on else w["duty_back"]
    for k in ("total", "net", "score"):
        if k in w:
            w[k] += d
    w["duty"] = 0.0 if on else w["duty_back"]
    w["combo_free"] = on


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
    # A title listing alternatives ("ESP32 / ESP32-C3 / ESP32-S3 …") proves
    # nothing about a variant: then the words must be in the variant name.
    if re.search(r"\s/\s", title):
        title = ""
    ok = [v for v in offer["variants"] if v["salable"] and v["price"] is not None
          and not any(w in v["label"].lower() for w in avoid)]
    # Every match word must stand in the variant name or the title. When the
    # product has several kinds of article ("S3 Expansion Board", "N16R8
    # soldered"), at least one match word must be in the variant name itself:
    # a title word alone says nothing about which variant has it.
    kinds = {deals.base_name(v["label"]) for v in ok}

    def fits(v):
        label = v["label"].lower()
        if not all(w in label or w in title for w in match):
            return False
        return len(kinds) <= 1 or not match or any(w in label for w in match)

    vs = [v for v in ok if fits(v)]
    if spec.get("sku_id"):
        vs = [v for v in offer["variants"] if v["sku_id"] == str(spec["sku_id"])
              and v["salable"] and v["price"] is not None]
    if not vs:
        return [], [f"{item_id}: no available variant matches '{spec.get('match', '')}'"]
    quote = _freight(offer, vs[0]["sku_id"])
    if spec.get("sku_id") and spec.get("qty"):
        q = int(spec["qty"])
        ship = 0.0 if offer["choice"] else deals.shipping_for(quote, q)
        ways = [{"lines": [(vs[0], q)], "line_ship": [ship], "pieces": vs[0]["pack"] * q,
                 "goods": vs[0]["price"] * q, "ship": ship, "duty": DUTY_PER_LINE,
                 "total": vs[0]["price"] * q + ship + DUTY_PER_LINE}]
    else:
        ways = deals.best_ways(vs, pieces, quote, offer["choice"], limit=10, duty=DUTY_PER_LINE)
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
        if item_id in COMBO_ITEMS:
            w["combo"], w["duty_back"] = True, w.get("duty") or 0.0
            if w["pieces"] >= COMBO_MIN_PIECES:     # 3+ pieces of this item alone carry the combo order
                _combo_free(w, True)
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


def _choice_basket(picks: list[dict], extra_choice: float = 0.0) -> tuple[float, float, float]:
    """(Choice goods of the picks, threshold, fee to pay) — `extra_choice` = other Choice goods in the same order."""
    choice = [p for p in picks if p["way"]["offer"]["choice"] and not p["way"].get("combo_free")]
    goods = sum(p["way"]["goods"] for p in choice)
    threshold = max((p["way"]["offer"]["shipping"].get("choice_threshold") or 0 for p in choice), default=0)
    fee = 0.0
    if choice and threshold and goods + extra_choice < threshold:
        fee = max((p["way"]["offer"]["shipping"].get("fee") or 0) for p in choice)
    return goods, threshold, fee


def _plan(specs: list[dict], day_cost: float = DAY_COST, extra_choice: float = 0.0) -> dict:
    """
    Best offer per list item, then the basket: Choice threshold, store
    discounts, delivery. `extra_choice`: Choice goods of other lines that go
    into the same order (checked cart lines that stay checked).
    """
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

    def select() -> None:
        # The parcel is complete when its slowest part arrives: a faster offer is
        # only worth more where it would otherwise be the slowest part. Floors come
        # from every part's own best before any re-scoring (order-independent);
        # unknown delivery times do not set a floor.
        for p in picks:
            for w in p["options"]:
                w["score"] = w["net"] + day_cost * (w["days"] if w["days"] is not None else UNKNOWN_DAYS)
            p["options"].sort(key=lambda w: w["score"])
        first_days = [p["options"][0]["days"] for p in picks]
        for i, p in enumerate(picks):
            floor = max((d for j, d in enumerate(first_days) if j != i and d), default=0)
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

    def combo_all(on: bool) -> None:
        for p in picks:
            for w in p["options"]:
                if on or w["pieces"] < COMBO_MIN_PIECES:     # a way of 3+ pieces carries the combo order by itself
                    _combo_free(w, on)

    # Kombi-Knaller: 3+ combo pieces in the combo order carry no import charge. Price
    # the Kombi offers that way when the best Kombi offers of the list reach 3 pieces,
    # and take the charge back when the picks end up with fewer.
    reach = sum(max((w["pieces"] for w in p["options"] if w.get("combo")), default=0) for p in picks)
    combo_all(reach >= COMBO_MIN_PIECES)
    select()
    if reach >= COMBO_MIN_PIECES and sum(p["way"]["pieces"] for p in picks if p["way"].get("combo")) < COMBO_MIN_PIECES:
        combo_all(False)
        select()
    for p in picks:
        if p["way"].get("combo") and not p["way"].get("combo_free"):
            p["way"]["combo"] = False       # too few Kombi pieces: a normal cart line, import charge included

    choice_goods, threshold, choice_fee = _choice_basket(picks, extra_choice)
    if choice_fee:
        # Below the threshold, Choice is not free: charge each Choice pick its
        # share of the fee and let a global offer win where it now is cheaper.
        share = choice_fee / max(1, sum(1 for p in picks if p["way"]["offer"]["choice"] and not p["way"].get("combo_free")))
        for p in picks:
            if not p["way"]["offer"]["choice"] or p["way"].get("combo_free"):
                continue
            alt = next((w for w in p["options"] if not w["offer"]["choice"]), None)
            if alt and alt["score"] < p["way"]["score"] + share:
                p["others"] = [p["way"]] + [w for w in p["others"] if w is not alt][:1]
                p["way"] = alt
        choice_goods, threshold, choice_fee = _choice_basket(picks, extra_choice)
    # Import charges: AliExpress pays them beyond the 9th Choice line. Once
    # the basket has that many, another Choice line costs no duty, while a
    # global line still pays its own: switch a global pick to its best Choice
    # offer where that is cheaper once the duty is left out.
    def n_lines(choice):
        return sum(len(p["way"]["lines"]) for p in picks
                   if bool(p["way"]["offer"]["choice"]) == choice and not p["way"].get("combo_free"))
    if n_lines(True) >= checkoutlib.CHOICE_DUTY_LINES:
        for p in picks:
            if p["way"]["offer"]["choice"] or p["way"].get("combo_free"):
                continue
            alt = next((w for w in p["options"] if w["offer"]["choice"] and not w.get("combo_free")), None)
            if alt and alt["score"] - alt.get("duty", 0.0) < p["way"]["score"]:
                p["others"] = [p["way"]] + [w for w in p["others"] if w is not alt][:1]
                p["way"] = alt
                p["why"] = (f"Choice: the basket has {checkoutlib.CHOICE_DUTY_LINES}+ Choice lines, "
                            "so this one carries no import charges and no own shipping")
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
    duty = checkoutlib.duty_for(n_lines(True), n_lines(False), DUTY_PER_LINE)
    dated = [p for p in picks if p["way"]["days"]]
    slowest = max(dated, key=lambda p: p["way"]["days"]) if dated else None
    kombi = [p for p in picks if p["way"].get("combo_free")]
    return {
        "combo_picks": kombi, "combo_pieces": sum(p["way"]["pieces"] for p in kombi),
        "combo_lines": sum(len(p["way"]["lines"]) for p in kombi),
        "combo_goods": sum(p["way"]["goods"] for p in kombi),
        "picks": picks, "notes": notes, "candidates": candidates,
        "choice_goods": choice_goods, "threshold": threshold, "choice_fee": choice_fee,
        "cart_choice": extra_choice,
        "stores": stores, "goods": goods, "ship": ship, "duty": duty, "saved": saved, "coupons": coupons,
        "total": goods + ship + duty - saved, "slowest": slowest,
    }


def _cart_choice_goods(cookies: dict[str, str], exclude: set[str] = frozenset()) -> float:
    """
    Choice goods of the checked cart lines (other products than `exclude`).
    Choice ships free per order, so they count towards the threshold when
    they are ordered together with the plan.
    """
    cart = _read_cart(cookies)
    return round(sum(cartlib.goods(ln) for ln in cart["lines"]
                     if ln["checked"] and ln["choice"] and ln["item_id"] not in exclude), 2)


PLANS_FILE = CREDENTIALS_PATH.parent / "aliexpress-plans.json"


def _attach_rounds(plan: dict) -> None:
    """Spread the Kombi picks over orders (rounds) and store them under a plan id for combo_round."""
    kombi = plan.get("combo_picks") or []
    plan["rounds"] = []
    if not kombi:
        return
    items = [{"pieces": p["way"]["pieces"], "lines": len(p["way"]["lines"]), "pick": p} for p in kombi]
    plan["rounds"] = [[i["pick"] for i in o] for o in _kombi_orders(items, COMBO_MAX_LINES or 10)]
    plan["id"] = f"plan-{uuid.uuid4().hex[:6]}"
    rounds = [{"added": False, "picks": [
        {"need": p["need"], "item_id": p["way"]["offer"]["item_id"], "pieces": p["way"]["pieces"],
         "lines": [{"sku_id": v["sku_id"], "label": v["label"], "qty": q, "price": v["price"]} for v, q in p["way"]["lines"]]}
        for p in r]} for r in plan["rounds"]]
    with _STATE_LOCK:
        try:
            store = json.loads(PLANS_FILE.read_text())
        except OSError:
            store = {}
        except ValueError:                                  # damaged file: keep it, start a new one
            PLANS_FILE.replace(PLANS_FILE.with_suffix(".json.bak"))
            store = {}
        store[plan["id"]] = {"list": plan.get("list") or "", "at": time.time(), "rounds": rounds}
        for old in sorted(store, key=lambda k: store[k]["at"])[:-20]:
            store.pop(old)
        private.write_json(PLANS_FILE, store)


def _combo_order_text(plan: dict) -> str:
    """The Kombi-Knaller part of a plan: its own order in the combo cart, import charge-free from 3 pieces."""
    n, lines = plan["combo_pieces"], plan["combo_lines"]
    text = (f"Kombi-Knaller order (combo cart, add_to_cart combo=True): {lines} line(s), {n} pcs, "
            f"{plan['combo_goods']:.2f} {CURRENCY} goods, no import charge (3+ pieces). It is ordered on its own checkout page, "
            "apart from the main cart (shipping and Choice threshold count there separately).")
    if COMBO_MAX_LINES and lines > COMBO_MAX_LINES:
        rounds = len(plan.get("rounds") or []) or -(-lines // COMBO_MAX_LINES)
        text += f" More than {COMBO_MAX_LINES} lines: order it in {rounds} rounds."
    else:
        text += " If its checkout says 'Limit exceeded, remove N items', order it in several rounds (each with 3+ pieces)."
    rounds = plan.get("rounds") or []
    if rounds:
        text += f"\nPlan {plan.get('id')}: {len(rounds)} round(s) of Kombi orders (each needs 3+ pieces; apply puts round 1 into the combo cart):"
        for i, r in enumerate(rounds, 1):
            text += f"\n  Round {i}: " + ", ".join(f"{p['need']} x{p['way']['pieces']}" for p in r)
        if len(rounds) > 1:
            text += f"\nAfter ordering a round: combo_round('{plan.get('id')}') puts the next round in and checks only it."
    return text + " The Kombi price on the product page is a hint: apply compares it with the combo cart."


def _fmt_plan(plan: dict) -> str:
    cur = CURRENCY
    out = [f"Plan: {len(plan['picks'])} item(s), "
           f"{sum(len(p['way']['lines']) for p in plan['picks'])} cart line(s)"]
    for i, p in enumerate(plan["picks"], 1):
        w, o = p["way"], p["way"]["offer"]
        kind = "Choice" if o["choice"] else f"global seller, {len(w['lines'])} line(s) × own shipping"
        if w.get("combo_free"):
            kind = "KOMBI-KNALLER (combo cart, no import charge)"
        parts = _way_view(w)["buy"]
        ship = "free" if o["choice"] else f"{w['ship']:.2f}"
        out.append(f"{i}. {p['need']}: {parts} = {w['pieces']} pcs — {o['title'][:60]} ({o['item_id']})")
        if p.get("why"):
            out.append(f"   why: {p['why']}")
        out.append(f"   goods {w['goods']:.2f} + shipping {ship}"
                   + (f" + import {w['duty']:.2f}" if w.get("duty") else "")
                   + (f" − {w['save']:.2f} ({w['save_how']})" if w["save"] else "")
                   + f" = {w['net']:.2f} {cur} · {w['days'] or '?'} days · {kind} · {o.get('store') or ''}"
                   + (f"\n   coupon to collect: −{w['coupon'][0]:.2f} ({w['coupon'][1]})" if w.get("coupon") else ""))
        for alt in p["others"]:
            ao = alt["offer"]
            out.append(f"   vs {ao['title'][:45]} ({ao['item_id']}): {alt['net']:.2f} {cur}, "
                       f"{alt['days'] or '?'} days{' Choice' if ao['choice'] else ''}"
                       f" → {alt['score'] - w['score']:+.2f} incl. delivery time")
    out.append("")
    if plan.get("combo_picks"):
        out.append(_combo_order_text(plan))
    if plan["threshold"]:
        if not plan["choice_fee"] and plan["cart_choice"] and plan["choice_goods"] < plan["threshold"]:
            st = (f"free shipping only together with the checked Choice lines already in the cart "
                  f"({plan['cart_choice']:.2f}) — order them in the same checkout")
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
               + (f" + {plan['duty']:.2f} import charges (estimate; checkout_preview has the real figure)"
                  if plan.get("duty") else "")
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


# The HTML pages view_cart and shop_list write. They are NOT published anywhere (they hold the
# user's cart): a small web server inside the MCP serves them on the local network, fixed names.
REPORT_DIR = reportserver.REPORT_DIR
REPORT_TOKEN_FILE = CREDENTIALS_PATH.parent / "aliexpress-report-token"


def _write_report(name: str, page: str) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / name
    if name.endswith(".html") and "<meta charset" not in page[:500].lower():
        page = '<meta charset="utf-8">\n' + page      # also right when opened as a file, without the server's header
    path.write_text(page, encoding="utf-8")
    link = ""
    try:
        # start() is False when the port is taken: fine if it is our server (another MCP process), not if a foreign service holds it
        if reportserver.start(REPORT_DIR, REPORT_TOKEN_FILE) or reportserver.alive(REPORT_TOKEN_FILE):
            link = reportserver.url(name, REPORT_TOKEN_FILE)
    except Exception as e:
        logger.debug("report server not started: %s", e)
    return (f"Page with pictures and links: {link or path}\n"
            + ("Give the user this address (served by the MCP on the local network, same address every time). "
               "Do not publish the page anywhere: it holds the cart and prices. " if link else
               f"File: {path} (the report server could not start: hand it over as a file, never publish it). ")
            + "The chat cannot show pictures, so point the user to the page.")


# Choice alternatives found for global seller lines, so view_cart does not
# search again on every call: {key: {"at": epoch, "alt": {...} | None}}.
ALT_CACHE_FILE = Path(os.environ.get(
    "ALIEXPRESS_ALT_CACHE", str(CREDENTIALS_PATH.parent / "aliexpress-alternatives.json")))
ALT_TTL = 6 * 3600


def _way_view(w: dict) -> dict:
    """A way as the pages and texts show it: picture, title, link, what to buy."""
    o = w["offer"]
    return {"img": next((v.get("image") for v, _ in w["lines"] if v.get("image")), None) or o.get("image"),
            "title": o["title"], "url": o["url"], "item_id": o["item_id"],
            "buy": " + ".join(f"{q}× {v['label']}" for v, q in w["lines"])}


def _alt_cache() -> dict:
    """The Choice-alternative cache without expired entries."""
    try:
        cache = json.loads(ALT_CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in cache.items() if time.time() - v.get("at", 0) < ALT_TTL}


def _line_deal(ln: dict, tag: dict, cache: dict) -> Optional[dict]:
    """
    Every other way to buy what this cart line is, priced like the line itself (via `cache`):
    {"now": net of the line, "best": cheapest single-line offer by normal net, "kombi": cheapest Kombi-Knaller offer
    by its net without import charge (only holds with 3+ pieces in the combo order)}; None when the line cannot be priced.
    """
    pieces = max(1, ln["qty"] * deals.pack_size(ln["variant"]))
    query = tag.get("query") or tag.get("need") or ln["title"][:60]
    match, avoid = tag.get("match", ""), tag.get("avoid", "")
    key = f"deal:{ln['item_id']}:{ln['sku_id']}:{pieces}:{query}:{match}:{avoid}"
    hit = cache.get(key)
    if hit:
        return hit["alt"]
    cur = _current_way(ln, pieces)
    if cur is None:
        return None
    rows, _, _ = _price_hits(_search(query), pieces, match, avoid, 8, False)
    best = kombi = None
    for w, h in rows:
        if w["pieces"] < pieces or len(w["lines"]) != 1 or w["lines"][0][1] <= 0:
            continue
        (v, q), = w["lines"]
        if str(w["offer"]["item_id"]) == str(ln["item_id"]) and str(v["sku_id"]) == str(ln["sku_id"]):
            continue
        def cand(price: float, combo: bool) -> dict:
            return {**_way_view(w), "sku_id": v["sku_id"], "qty": q, "cost": round(price, 2), "days": w.get("days"),
                    "ambiguous": bool(v.get("ambiguous")), "combo": combo, "pieces": w["pieces"],
                    "choice": bool(w["offer"].get("choice")), "lines": [{"sku_id": v["sku_id"], "qty": q}]}
        if best is None or w["net"] < best["cost"]:
            best = cand(w["net"], False)
        if h.get("combo"):
            kn = w["net"] - (0 if w.get("combo_free") else w.get("duty_back", 0.0))
            if kombi is None or kn < kombi["cost"]:
                kombi = cand(kn, True)
                kombi["net"] = round(w["net"], 2)
    alt = {"now": cur["net"], "best": best, "kombi": kombi}
    cache[key] = {"at": time.time(), "alt": alt}
    return alt


def _merge_candidate(item_id: str, pieces: int, tag: dict, cache: dict) -> Optional[dict]:
    """
    The cheapest way to cover `pieces` pieces in ONE cart line of this product (via `cache`), or None.
    Priced by `_ways_for`, the pricing the planner uses (pack ladder, shipping, store discounts, import charge, `match`/`avoid`).
    """
    key = f"merge:{item_id}:{pieces}:{tag.get('match', '')}:{tag.get('avoid', '')}"
    hit = cache.get(key)
    if hit:
        return hit["alt"]
    ways, _ = _ways_for(item_id, {"pieces": pieces, "match": tag.get("match", ""), "avoid": tag.get("avoid", "")}, 0.0)
    ways = [w for w in ways if len(w["lines"]) == 1 and w["pieces"] >= pieces and w["lines"][0][1] > 0]
    alt = None
    if ways:
        w = min(ways, key=lambda w: (w["net"], w["pieces"]))
        (v, q), = w["lines"]
        alt = {**_way_view(w), "sku_id": v["sku_id"], "qty": q, "cost": round(w["net"], 2), "pieces": w["pieces"]}
    cache[key] = {"at": time.time(), "alt": alt}
    return alt


def _merge_split_lines(pairs: list[tuple[dict, dict]], cache: dict) -> list[dict]:
    """
    Cart lines of ONE need (same shopping list and the same item, query or need name before " (") that together are
    cheaper as a single line of one of their products. One dict per group that is worth merging:
    {lines, win (candidate), keep (cart line that already is the winner, or None), split, saving}.
    """
    parent: dict[int, int] = {}
    def find(i: int) -> int:
        while parent.setdefault(i, i) != i:
            i = parent[i]
        return i
    keys = []
    for ln, tag in pairs:
        base = str(tag.get("need") or "").split(" (")[0].strip().lower()
        keys.append((tag.get("list") or "", str(ln["item_id"]), str(tag.get("query") or "").strip().lower(), base))
    for i, (ln, tag) in enumerate(pairs):
        if not tag.get("need"):
            continue
        for j in range(i):
            if not pairs[j][1].get("need") or keys[i][0] != keys[j][0]:
                continue
            same_item = keys[i][1] == keys[j][1]
            if same_item and deals.base_name(ln.get("variant")) != deals.base_name(pairs[j][0].get("variant")):
                continue   # another variant of the item is another article, not the same need
            if same_item or (keys[i][2] and keys[i][2] == keys[j][2]) or (keys[i][3] and keys[i][3] == keys[j][3]):
                parent[find(i)] = find(j)
    members: dict[int, list[int]] = {}
    for i in range(len(pairs)):
        members.setdefault(find(i), []).append(i)
    out = []
    for idx in members.values():
        if len(idx) < 2:
            continue
        lns = [pairs[i][0] for i in idx]
        tag = next((pairs[i][1] for i in idx if pairs[i][1].get("match")), pairs[idx[0]][1])
        total = sum(ln["qty"] * deals.pack_size(ln.get("variant")) for ln in lns)
        split = sum(cartlib.goods(ln) + (ln.get("ship") or 0) + DUTY_PER_LINE for ln in lns)
        best = None
        for item_id in dict.fromkeys(str(ln["item_id"]) for ln in lns):
            try:
                cand = _merge_candidate(item_id, total, tag, cache)
            except Exception as e:
                logger.debug("merge candidate %s failed: %s", item_id, e)
                cand = None
            if cand and (best is None or cand["cost"] < best["cost"]):
                best = cand
        if not best or split - best["cost"] <= 0.005:
            continue
        keep = next((ln for ln in lns if str(ln["item_id"]) == str(best["item_id"]) and str(ln["sku_id"]) == str(best["sku_id"])), None)
        out.append({"lines": lns, "win": best, "keep": keep, "split": round(split, 2), "saving": round(split - best["cost"], 2),
                    "total": total, "need": str(tag.get("need") or lns[0]["title"][:40]).split(" (")[0].strip()})
    return out


def _date_in(days: Optional[int]) -> Optional[str]:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400 * days)) if days else None


def _decisions(cart: dict, tags: dict[str, dict], lines: list[dict], alternatives: bool) -> list[dict]:
    """
    What the user can decide about a list, each with its effect on price and
    delivery: global lines → Choice, dropping optional parts, leftovers.
    """
    out = []
    latest_of = lambda lns: max((ln["delivery"][1] for ln in lns if ln["delivery"][1]), default=None)
    latest = latest_of(lines)
    cache = _alt_cache() if alternatives else {}
    for ln in lines if alternatives else []:
        if ln["choice"]:
            continue
        t = tags[ln["cart_id"]]
        try:
            alt = (_line_deal(ln, t, cache) or {}).get("best")
        except Exception as e:
            logger.debug("alternative for %s failed: %s", ln["cart_id"], e)
            alt = None
        if not alt or not alt["choice"]:
            continue
        now = cartlib.goods(ln) + (ln["ship"] or 0) + DUTY_PER_LINE
        lo, hi = alt["cost"] - now, alt["cost"] + DUTY_PER_LINE - now
        rest = latest_of([x for x in lines if x is not ln])
        new_latest = max(d for d in (rest, _date_in(alt["days"])) if d) if (rest or alt["days"]) else None
        out.append({
            "kind": "choice", "cart_id": ln["cart_id"],
            "title": f"{t.get('need') or ln['title'][:40]} über Choice kaufen",
            "img": alt["img"], "link": alt["url"], "link_text": alt["title"],
            "facts": [f"{alt['buy']} für {report.eur(alt['cost'])} statt {report.eur(now)} "
                      f"(jetzt: Ware {report.eur(cartlib.goods(ln))} + Versand {report.eur(ln['ship'] or 0)}"
                      f" + Abgabe {report.eur(DUTY_PER_LINE)})",
                      f"Lieferung in etwa {alt['days']} Tagen statt bis {_day(ln['delivery'][1])}"],
            "price": (lo, hi),
            "date": (_day(latest), _day(new_latest)) if new_latest and new_latest != latest else None,
            "warn": "Variantenname kommt mehrfach vor — Bild prüfen" if alt["ambiguous"] else None,
            "alt_days": alt["days"],
            "do": f"shop_list apply: item {alt['item_id']} " + ", ".join(f"sku {x['sku_id']}×{x['qty']}" for x in alt["lines"])
                  + f", then remove_from_cart {ln['cart_id']}",
        })
    if cache:
        private.write_json(ALT_CACHE_FILE, cache)
    switch = list(out)
    if len(switch) > 1:
        moved = {d["cart_id"] for d in switch}
        days = [d["alt_days"] for d in switch if d.get("alt_days")]
        rest = latest_of([x for x in lines if x["cart_id"] not in moved])
        new_latest = max([x for x in [rest] + [_date_in(n) for n in days] if x], default=None)
        out.insert(0, {
            "kind": "choice", "cart_id": None,
            "title": "Alle Teile von globalen Händlern über Choice kaufen",
            "facts": [", ".join(d["title"].replace(" über Choice kaufen", "") for d in switch)
                      + ": eine Sendung, kein eigener Versand."],
            "price": (sum(d["price"][0] for d in switch), sum(d["price"][1] for d in switch)),
            "date": (_day(latest), _day(new_latest)) if new_latest and new_latest != latest else None,
            "do": " ; ".join(d["do"] for d in switch),
        })
    opt = [ln for ln in lines if tags[ln["cart_id"]].get("optional")]
    if opt:
        goods = sum(cartlib.goods(ln) + (ln["ship"] or 0) for ln in opt)
        rest = latest_of([x for x in lines if x not in opt])
        out.append({
            "kind": "drop", "title": f"{len(opt)} Vergleichsteile weglassen",
            "facts": [", ".join(tags[ln["cart_id"]].get("need") or ln["title"][:30] for ln in opt)],
            "price": (-(goods + DUTY_PER_LINE * len(opt)), -goods),
            "date": (_day(latest), _day(rest)) if rest and rest != latest else None,
            "do": "remove_from_cart " + ", ".join(ln["cart_id"] for ln in opt),
        })
    for ln, twin in cartlib.leftovers(lines, cartlib.by_list(cart, tags)[1]):
        out.append({
            "kind": "tidy", "title": "Doppeltes Teil aus dem Korb löschen",
            "facts": [f"„{ln['title'][:50]}“ liegt zusätzlich im Korb, wie "
                      f"{tags[twin['cart_id']].get('need') or 'ein Listenteil'}. Es ist nicht im Kassen-Link, "
                      "ändert den Preis also nicht."],
            "price": None, "date": None, "do": f"remove_from_cart {ln['cart_id']}",
        })
    return out


def _cart_report(cart: dict, tags: dict[str, dict], previews: dict[str, dict], summary: str = "",
                 options: Optional[list[dict]] = None, alternatives: bool = True,
                 combined: Optional[dict] = None, combo_lines: Optional[list[dict]] = None) -> tuple[str, list[dict]]:
    """One page for every listed line (`combined` = the checkout preview over all of them);
    `combo_lines` = the Kombi-Knaller cart, shown as its own basket."""
    by_list, rest = cartlib.by_list(cart, tags)
    listed = [(name, ln) for name, lns in by_list.items() for ln in lns]
    lines = [ln for _, ln in listed]
    dec = _decisions(cart, tags, lines, alternatives) if lines else []
    for o in options or []:
        dec.append({"kind": "agent", "title": o.get("title", ""), "facts": [o.get("text", "")] if o.get("text") else [],
                    "img": o.get("img"), "link": o.get("link"), "link_text": o.get("link_text") or o.get("link"),
                    "price": (o["delta"], o["delta"]) if isinstance(o.get("delta"), (int, float)) else None,
                    "date": None, "do": o.get("do")})
    p = combined or {}
    dated = [ln for ln in lines if ln["delivery"][1]]
    slow = max(dated, key=lambda ln: ln["delivery"][1]) if dated else None
    # rows vs checkout on goods + shipping only: independent of how the import charge is split
    mismatch = (sum(cartlib.goods(ln) + (ln["ship"] or 0) for ln in lines)
                - (p.get("subtotal") or 0.0) - (p.get("shipping") or 0.0)) if p.get("total") is not None else 0.0
    names = list(by_list)
    items = []
    for name, ln in sorted(listed, key=lambda x: (x[1]["choice"], -cartlib.goods(x[1]))):
        pieces = ln["qty"] * deals.pack_size(ln["variant"])
        items.append({
            "img": ln.get("image"), "need": tags[ln["cart_id"]].get("need") or ln["title"][:40],
            "title": ln["title"], "url": f"{BASE_URL}/item/{ln['item_id']}.html", "qty": ln["qty"],
            "variant": ln["variant"] or "Standard", "cost": cartlib.goods(ln) + (ln["ship"] or 0),
            "extra": f"inkl. {report.eur(ln['ship'])} Versand" if ln["ship"] else "",
            "pieces": pieces, "unit": cartlib.goods(ln) / max(1, pieces),
            "kind": "Choice" if ln["choice"] else "global", "list": name if len(names) > 1 else "",
            "until": _day(ln["delivery"][1]), "slowest": slow is ln,
            "optional": bool(tags[ln["cart_id"]].get("optional")),
        })
    unlisted = [{"img": ln.get("image"), "title": ln["title"], "url": f"{BASE_URL}/item/{ln['item_id']}.html",
                 "qty": ln["qty"], "cost": cartlib.goods(ln)} for ln in rest]
    page = {
        "name": names[0] if len(names) == 1 else "Übersicht", "total": p.get("total"),
        "goods": p.get("subtotal") or 0.0, "ship": p.get("shipping") or 0.0,
        "duty": (p.get("duty") or 0.0) - (p.get("duty_off") or 0.0), "discount": p.get("discounts") or 0.0,
        "mismatch": mismatch if abs(mismatch) > 0.05 else 0.0,
        "latest": _day(slow["delivery"][1]) if slow else None,
        "slowest": (tags[slow["cart_id"]].get("need") or slow["title"][:30]) if slow else None,
        "summary": summary, "decisions": dec, "items": items, "unlisted": unlisted,
        "checkout_url": cartlib.checkout_url([ln["cart_id"] for ln in lines]),
        "checkout_all_url": cartlib.checkout_url([ln["cart_id"] for ln in lines + rest]),
        "combo": _combo_page(cart, combo_lines, tags, names),
    }
    urls = ([it["img"] for it in items] + [o["img"] for o in unlisted] + [d.get("img") for d in dec]
            + [r.get("img") for r in (page["combo"] or {}).get("rows", [])])
    imgs = report.inline_images(urls, _fetch_image)
    for it in items:
        it["img"] = imgs.get(it["img"])
    for d in dec:
        d["img"] = imgs.get(d.get("img")) if d.get("img") else None
    for o in unlisted:
        o["img"] = imgs.get(o["img"])
    for r_ in (page["combo"] or {}).get("rows", []):
        r_["img"] = imgs.get(r_.get("img"))
    return _write_report("cart.html", report.cart_page(page, time.strftime("%d.%m.%Y %H:%M"))), dec


def _plan_report(plan: dict) -> str:
    def offer(w: dict, diff: float = 0.0) -> dict:
        return {**_way_view(w), "net": w["net"], "unit": w["net"] / max(1, w["pieces"]),
                "kind": "Choice" if w["offer"]["choice"] else "global",
                "days": f"{w['days']} Tage" if w["days"] else "Lieferzeit ?", "diff": diff}
    picks = [{"need": p["need"], "pieces": p["spec"].get("pieces", 1), "why": p.get("why"),
              "winner": offer(p["way"]),
              "alts": [offer(a, a["net"] - p["way"]["net"]) for a in p["others"]]} for p in plan["picks"]]
    imgs = report.inline_images([x["img"] for p in picks for x in [p["winner"], *p["alts"]]], _fetch_image)
    for p in picks:
        for x in [p["winner"], *p["alts"]]:
            x["img"] = imgs.get(x["img"])
    sl = plan["slowest"]
    total = {"goods": plan["goods"], "ship": plan["ship"], "duty": plan.get("duty", 0.0), "saved": plan["saved"],
             "total": plan["total"], "slowest": f"{sl['need']}, bis {sl['way']['days']} Tage" if sl else None}
    return _write_report("plan.html", report.plan_page(plan.get("list") or "Einkaufsliste", picks, total,
                                                       time.strftime("%d.%m.%Y %H:%M")))


def _day(date: Optional[str]) -> str:
    return f"{date[8:10]}.{date[5:7]}." if date and len(date) >= 10 else "?"


def _cart_notes(cart: dict, tags: dict[str, dict], lines: list[dict]) -> list[str]:
    """What the user should know about a list's lines: costly global lines, ticks, leftovers."""
    notes = []
    for ln in lines:
        if not ln["choice"]:
            notes.append(f"{tags[ln['cart_id']].get('need') or ln['title'][:30]} kommt von einem globalen Händler: "
                         f"{report.eur(cartlib.goods(ln))} Ware + {report.eur(ln['ship'] or 0)} Versand + "
                         f"{report.eur(DUTY_PER_LINE)} Einfuhrabgabe.")
    for ln, twin in cartlib.leftovers(lines, cartlib.by_list(cart, tags)[1]):
        notes.append(f"„{ln['title'][:40]}“ liegt zusätzlich im Korb, nicht auf der Liste: dasselbe Teil wie "
                     f"{tags[twin['cart_id']].get('need') or 'ein Listenteil'}, wohl ein Rest.")
    return notes


def _chat_cart(cart: dict, tags: dict[str, dict], previews: dict[str, dict], combined: Optional[dict] = None) -> str:
    """A few speakable lines: the one total, then per list what checkout charges and the hints. Pictures and links are on the page."""
    out = []
    by_list, rest = cartlib.by_list(cart, tags)
    if combined and combined.get("total") is not None and len(by_list) > 1:
        out.append(f"Alles zusammen an der Kasse {report.eur(combined['total'])}"
                   + (f", alles da bis {_day(combined['latest'])}" if combined.get("latest") else "")
                   + ". Die Zahlen je Liste unten sind einzelne Kassen und ergeben zusammen etwas anderes (gemeinsame Abgabe).")
    for name, lines in by_list.items():
        p = previews.get(name)
        line = f"Liste „{name}“: {len(lines)} Teile"
        if p and p.get("total") is not None:
            line += (f", an der Kasse {report.eur(p['total'])} (Ware {report.eur(p['subtotal'] or 0)}, Versand "
                     f"{report.eur(p['shipping'] or 0)}, Einfuhrabgaben {report.eur(p['duty'] - p['duty_off'])}"
                     + (f", Rabatte −{report.eur(p['discounts'])}" if p["discounts"] else "") + ")"
                     + (f", alles da bis {_day(p['latest'])}" if p.get("latest") else ""))
        out.append(line.rstrip(".") + ".")
        out += [f"- {n}" for n in _cart_notes(cart, tags, lines)]
    if rest:
        out.append(f"Sonst im Korb: {len(rest)} Zeilen auf keiner Liste ("
                   + ", ".join(ln["title"][:25] for ln in rest[:5]) + ("…" if len(rest) > 5 else "") + ").")
    return "\n".join(out)


def _chat_plan(plan: dict) -> str:
    """One speakable line per part (winner, price, the runner-up's extra cost), then the total."""
    out = []
    for p in plan["picks"]:
        w, o = p["way"], p["way"]["offer"]
        kind = "Choice" if o["choice"] else "globaler Händler"
        if w.get("combo_free"):
            kind = "Kombi-Knaller, keine Einfuhrabgabe"
        line = (f"- {p['need']}: {o['title'][:40]}, {report.eur(w['net'])} ({kind}, "
                f"{w['days'] or '?'} Tage)")
        if p["others"]:
            nxt = p["others"][0]
            line += f"; nächstbestes {report.eur(nxt['net'])} (+{report.eur(nxt['net'] - w['net'])})"
        out.append(line)
    if plan.get("combo_picks"):
        out.append(f"Davon Kombi-Knaller: {plan['combo_lines']} Zeilen, {plan['combo_pieces']} Stück, eigene Bestellung im Kombi-Korb.")
    out.append(f"Gesamt {report.eur(plan['total'])}: Ware {report.eur(plan['goods'])}, Versand {report.eur(plan['ship'])}"
               + (f", Einfuhrabgaben {report.eur(plan['duty'])}" if plan.get("duty") else "")
               + (f", Rabatte −{report.eur(plan['saved'])}" if plan["saved"] else "") + ".")
    return "\n".join(out)


def _apply_combo(cookies: dict[str, str], name: str, kombi: dict[tuple, dict]) -> list[str]:
    """Put the plan's Kombi lines into the combo cart (it ticks new lines itself); compare price and quantity."""
    log = []
    try:
        every = _read_cart(cookies, COMBO)["lines"]
    except Exception as e:
        return [f"FAILED to read the Kombi-Knaller cart: {e} - no Kombi line added"]
    have = {(ln["item_id"], ln["sku_id"]): ln for ln in every if ln["valid"]}
    known = {ln["cart_id"] for ln in every}
    for (iid, sku), e in kombi.items():
        ln = have.get((iid, sku))
        if ln and ln["qty"] >= e["qty"] and ln["checked"]:
            cid = ln["cart_id"]
            log.append(f"Kombi: {e['need']} already in the combo cart ({ln['qty']}x, cart_id {cid})")
        else:
            qty = max(e["qty"], ln["qty"] if ln else 0)    # adding a variant that is there replaces its line: give the total
            try:
                cid = _cart_add(cookies, iid, sku, qty, known=known, business=COMBO)
                known.add(cid)
            except Exception as ex:
                log.append(f"FAILED to add Kombi {e['need']} ({e['label']}): {ex}")
                continue
            log.append(f"Kombi: added {qty}x {e['label']} to the combo cart: {e['need']} (cart_id {cid})")
            if ln:
                _remember_lines({}, drop=[ln["cart_id"]])
        if name:
            _remember_lines({cid: {"list": name, "need": e["need"], "item_id": iid, "sku_id": sku, "own": True, "combo": True}})
    try:
        now = {(ln["item_id"], ln["sku_id"]): ln for ln in _read_cart(cookies, COMBO)["lines"]}
    except Exception as e:
        return log + [f"Kombi: cart not re-read ({e})"]
    for key, e in kombi.items():
        ln = now.get(key)
        if not ln:
            log.append(f"Kombi {e['need']}: not in the combo cart afterwards")
        elif ln["price"] is not None and e["price"] and abs(ln["price"] - e["price"]) > 0.05:
            log.append(f"Kombi {e['need']}: {ln['price']:.2f} each in the combo cart, {e['price']:.2f} planned from the product page")
    pick = [ln for ln in now.values() if ln["valid"] and ln["checked"]]
    log.append(f"Kombi cart now: {len(pick)} checked line(s) = {sum(ln['qty'] for ln in pick)} pcs "
               "(a checked Kombi line of another list ships in the same order)")
    return log


def _apply_plan(cookies: dict[str, str], plan: dict) -> str:
    """Put the plan into the cart, drop the MCP's own replaced lines, check exactly the plan, verify."""
    cart = _read_cart(cookies)
    by_key = {(ln["item_id"], ln["sku_id"]): ln for ln in cart["lines"]}
    known = {ln["cart_id"] for ln in cart["lines"]}
    store = _line_store()
    mine = {k for k, v in store.items() if not v.get("own")}
    # One cart line per item+sku: add up what several picks want of it.
    wanted: dict[tuple, dict] = {}
    kombi: dict[tuple, dict] = {}
    for p in plan["picks"]:
        w = p["way"]
        for (v, q), ship in zip(w["lines"], w["line_ship"]):
            if w.get("combo_free"):
                if len(plan.get("rounds") or []) > 1 and (p["need"], w["offer"]["item_id"]) not in {
                        (q["need"], q["way"]["offer"]["item_id"]) for q in plan["rounds"][0]}:
                    continue                    # a later round: combo_round puts it in when its turn comes
                key = (w["offer"]["item_id"], v["sku_id"])
                e = kombi.setdefault(key, {"need": p["need"], "label": v["label"], "qty": 0, "price": v["price"]})
                e["qty"] += q
                continue
            key = (w["offer"]["item_id"], v["sku_id"])
            e = wanted.setdefault(key, {"need": p["need"], "label": v["label"], "qty": 0, "ship": 0.0,
                                        "days": w["days"], "choice": w["offer"]["choice"], "price": v["price"]})
            e["qty"] += q
            e["ship"] += ship
    want: list[str] = []
    expect: dict[str, dict] = {}
    edits: dict[str, Any] = {}
    log = []
    for (iid, sku), e in wanted.items():
        ln = by_key.get((iid, sku))
        if ln and ln["cart_id"] in mine:
            cid = ln["cart_id"]
            if ln["qty"] != e["qty"]:
                edits[cid] = _edit_qty(e["qty"])
        elif ln:
            # The user's own line of this variant: never resized by a plan.
            cid = ln["cart_id"]
            if ln["qty"] < e["qty"]:
                log.append(f"NOTE {e['need']}: your own line has {ln['qty']}× {e['label']}, the plan needs "
                           f"{e['qty']} — not changed; raise it yourself or update_cart_quantity")
        else:
            try:
                cid = _cart_add(cookies, iid, sku, e["qty"], known=known)
                known.add(cid)  # F2: keep known current so later adds in same run see it
                log.append(f"added {e['qty']}× {e['label']}: {e['need']}")
            except Exception as ex:
                log.append(f"FAILED to add {e['need']} ({e['label']}): {ex}")
                continue
        want.append(cid)
        expect[cid] = e
    # Tag every plan line with its list and need; the user's own lines too
    # (marked `own`, so no plan ever deletes them).
    name = plan.get("list") or ""
    tags: dict[str, dict] = {}
    drop_tags: list[str] = []
    if name:
        for (iid, sku), e in wanted.items():
            ln = by_key.get((iid, sku))
            cid = next((c for c in want if expect.get(c) is e), None)
            if not cid:
                continue
            tag = {"list": name, "need": e["need"]}
            if ln and ln["cart_id"] not in mine:
                tag.update(item_id=iid, sku_id=sku, own=True)
            tags[cid] = tag
        # The user's own lines that were on this list and are no longer: untag.
        drop_tags = [c for c, v in store.items() if v.get("own") and not v.get("combo") and v.get("list") == name and c not in want]
    drop = [ln for ln in cart["lines"]
            if ln["cart_id"] in mine and ln["cart_id"] not in want
            and (ln["item_id"] in plan["candidates"] or (name and store.get(ln["cart_id"], {}).get("list") == name))]
    edits.update({ln["cart_id"]: _edit_delete for ln in drop})
    done, failed = _cart_line_action(cookies, edits) if edits else ([], {})
    _remember_lines(tags, drop=drop_tags + [ln["cart_id"] for ln in drop if ln["cart_id"] in done])
    for (iid, sku), e in wanted.items():
        ln = by_key.get((iid, sku))
        if ln and ln["cart_id"] in edits:
            log.append(f"quantity {ln['qty']}→{e['qty']}: {e['need']} ({e['label']})" if ln["cart_id"] in done
                       else f"FAILED quantity for {e['need']}: {failed.get(ln['cart_id'])}")
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
    combo_log = _apply_combo(cookies, plan.get("list") or "", kombi) if kombi else []
    if not want:
        # Nothing for the main cart: leave its selection alone (an empty `only` would uncheck every line there).
        return ("Applied:\n" + "\n".join(f"- {x}" for x in log + combo_log)
                + "\n\nNothing of this plan goes into the main cart, its selection is unchanged. Order the Kombi-Knaller "
                  "cart on its own checkout page (view_cart shows the link).")
    sel, after = _select(cookies, want, only=True)
    lines = {ln["cart_id"]: ln for ln in after["lines"]}
    diffs = []
    for cid, e in expect.items():
        ln = lines.get(cid)
        if not ln:
            diffs.append(f"{e['need']}: line {cid} not in the cart")
            continue
        if ln["price"] is not None and e["price"] and abs(ln["price"] - e["price"]) > 0.05:
            diffs.append(f"{e['need']}: {ln['price']:.2f} each in the cart, {e['price']:.2f} on the product page")
        if ln["qty"] < e["qty"]:
            diffs.append(f"{e['need']}: {ln['qty']} in the cart, the plan needs {e['qty']}")
        if ln["ship"] is not None and not e["choice"] and abs(ln["ship"] - e["ship"]) > 0.5:
            diffs.append(f"{e['need']}: shipping {ln['ship']:.2f} in the cart, planned {e['ship']:.2f}")
        days = ln["days"][1]
        if days and e["days"] and abs(int(days) - int(e["days"])) > 3:
            diffs.append(f"{e['need']}: {days} days in the cart, planned {e['days']}")
    out = ["Applied:"] + [f"- {x}" for x in log + combo_log] + ["", sel, ""]
    out.append("Plan vs cart: " + ("matches (price, shipping within 0.50, delivery within 3 days)." if not diffs
                                   else "differs:\n" + "\n".join(f"- {d}" for d in diffs)))
    # The checkout link holds every plan line, also those beyond the 10 the
    # cart page keeps checked: price exactly those at checkout.
    try:
        out += ["", _checkout_preview_text(cookies, [c for c in want if c in lines])]
        return "\n".join(out)
    except Exception as ex:
        out.append(f"(checkout preview failed: {ex}; estimate from the cart instead)")
    sub = [dict(lines[c], checked=True) for c in want if c in lines]
    goods = sum(cartlib.goods(ln) for ln in sub)
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


@tool(slow=True)
def shop_list(items: list[dict], apply: bool = False, day_cost: float = DAY_COST, choice_only: bool = False,
              list_name: str = "") -> str:
    """
    From a shopping list to a checked cart: finds the best offer for every
    item, prices the basket as checkout will (shipping per cart line for
    global sellers, Choice free-shipping threshold, store discounts and
    coupons, delivery time), and with apply=True puts exactly that into the
    cart. Never buys: the result ends with the checkout link for the user.

    Each item is a dict:
        need:      what it is, for the report ("ESP32-S3 N16R8 dev board")
        pieces:    how many pieces are needed (packs are counted in pieces)
        query:     search term — of its 60 hits, the `candidates` (default 5, 10 when
                   pieces > 1) cheapest whose title has the match words compete
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

    apply=True: adds or re-sizes the planned lines and removes the lines
    this MCP added earlier for this list's products or `list_name` that the
    plan replaced — never the user's own lines, never other lists. Collects
    the store coupon worth having, ticks the plan, prices it at checkout,
    returns the checkout link with every plan line and compares plan and cart.

    Every cart line pays an import charge (about 3.57 € to DE) whatever its
    quantity; the plan counts it, so one bigger pack beats two lines.

    Kombi-Knaller hits (marked in search_products, so search the part first) go
    into the combo cart: 3+ pieces there pay no import charge. The plan prices
    them that way once its Kombi picks reach 3 pieces (otherwise the charge
    stays), lists them as their own order and apply puts them into the combo
    cart. Their checkout is a separate page, in several rounds if it says
    "Limit exceeded".

    Args:
        items: The shopping list (see above).
        apply: Put the plan into the cart.
        day_cost: EUR per delivery day when ranking (default 0.15).
        choice_only: `seller: "choice"` for every item.
        list_name: Name of the shopping list ("voice satellite"). With apply,
            every plan line is tagged with it and its need, so view_cart and
            checkout_preview show the list apart from the rest of the cart.
    """
    if choice_only:
        items = [{**it, "seller": "choice"} for it in items]
    cookies = load_cookies()
    extra = 0.0
    if cookies and not apply:
        # Without apply the plan joins whatever is checked; apply unchecks it.
        try:
            extra = _cart_choice_goods(cookies, {str(i) for it in items for i in it.get("item_ids") or []})
        except Exception as e:
            logger.debug("cart read for the Choice threshold failed: %s", e)
    try:
        plan = _plan(items, day_cost, extra_choice=extra)
    except PermissionError:
        return AUTH_EXPIRED_MSG
    plan["list"] = list_name.strip()
    _attach_rounds(plan)
    if plan["picks"]:
        try:
            page = _plan_report(plan)
        except Exception as e:
            page = f"(page not written: {e})"
        text = ("## For the user — say this in a few sentences\n" + _chat_plan(plan) + "\n\n" + page
                + "\n\n## Details (for you)\n" + _fmt_plan(plan))
    else:
        text = _fmt_plan(plan)
    if not apply or not plan["picks"]:
        return text
    if not cookies:
        return text + "\n\n" + AUTH_EXPIRED_MSG
    # A variant whose name repeats in its product ("8 bit" = ring and strip)
    # is not put into the cart blind: look at the pictures, then pass sku_id.
    blind = [p for p in plan["picks"] if not p["spec"].get("sku_id")
             and any(v.get("ambiguous") for v, _ in p["way"]["lines"])]
    if blind:
        return text + "\n\nNot applied: the chosen variant's name repeats in " + ", ".join(
            f"{p['need']} ({p['way']['offer']['item_id']}: '{p['way']['lines'][0][0]['label']}')" for p in blind
        ) + ". Call get_variant_images, pick the right picture and pass its sku_id (with qty) for that item."
    try:
        return text + "\n\n" + _apply_plan(cookies, plan)
    except Exception as e:
        return text + f"\n\nApplying failed: {e}"


@tool()
def get_shipping_estimate(item_id: str) -> str:
    """
    Check shipping time and cost for a product to the configured country
    (ALIEXPRESS_COUNTRY, default DE).

    Args:
        item_id: AliExpress item ID (e.g., "1005007655628250")
    """
    offer = _pdp_offer(item_id)
    if not offer:
        return f"Could not fetch shipping data for item {item_id}." + blocked_msg()
    quote = _freight(offer)
    text = deals.fmt_quote(quote, CURRENCY) if quote else _fmt_shipping(offer["shipping"])
    return f"Shipping to {COUNTRY} for item {item_id}: {text}"


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


_CART_PACE_LOCK = threading.Lock()


def _cart_pace() -> None:
    global _last_cart_call
    with _CART_PACE_LOCK:      # tools run in threads: one cart call at a time passes the gap
        wait = _last_cart_call + CART_CALL_DELAY_S - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_cart_call = time.time()


COMBO = "nn_mix"   # the Kombi-Knaller cart ("Combo Blast cart"): 3+ items, no import charge


def _cart_base_payload(business: str = "") -> dict[str, Any]:
    biz = {"platformType": "DESKTOP", "pcChoiceNewCart": 1}
    out = {"_currency": CURRENCY, "shipToCountry": COUNTRY, "_saasRegion": "aeg", "locale": "en_US"}
    if business:
        biz.update(localCart="GLOBAL_YELLOW_PC", selectIds="", isNewStyle=True, sourceBizPage="nnHsCode")
        out["business"] = business
    return {**out, "bizParams": json.dumps(biz)}


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


def _cart_render(cookies: dict[str, str], business: str = "") -> dict:
    """Page 1 of the cart (or of channel cart `business`) as a droplet snapshot (the web page's format, in CURRENCY)."""
    _cart_pace()
    return _cart_check(mtop_call(
        "mtop.aliexpress.trade.cart.render", "1.0",
        {"platformType": "DESKTOP", **_cart_base_payload(business)},
        cookies=cookies, referer=CART_REFERER,
    ))


def _cart_async(state: dict, operator: str, block: dict, cookies: dict[str, str], next_page: bool = False,
                business: str = "") -> dict:
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
    payload = {"compress": True, "params": params, "nextPage": next_page, **_cart_base_payload(business)}
    _cart_pace()
    return _cart_check(mtop_call(
        "mtop.aliexpress.trade.cart.async", "1.0", payload,
        cookies=cookies, referer=CART_REFERER, method="POST",
    ))


def _cart_pages(cookies: dict[str, str], business: str = "") -> tuple[dict, list[dict]]:
    """
    Every page of the cart, plus one snapshot merged over them that actions
    are sent against (next pages must be requested from it as well).
    """
    state = _cart_render(cookies, business)
    pages, merged = [state], cartlib.merge(None, state)
    for n in range(1, CART_MAX_PAGES + 1):
        pg = cartlib.pagination(state)
        if not pg or not pg[1].get("hasMore"):
            break
        if n == CART_MAX_PAGES:
            logger.warning("cart has more than %d pages; the rest is not read", CART_MAX_PAGES)
            break
        state = _cart_async(merged, pg[0], pg[1], cookies, next_page=True, business=business)
        pages.append(state)
        merged = cartlib.merge(merged, state)
    return merged, pages


def _edit_qty(qty: int):
    def edit(fields):
        fields["operationType"] = "update_quantity"
        fields["quantityView"] = {"current": int(qty)}
    return edit


def _edit_check(on: bool):
    def edit(fields):
        fields["operationType"] = "checkbox"
        fields.setdefault("checkbox", {})["selected"] = on
    return edit


def _edit_delete(fields):
    fields["operationType"] = "delete"


LINE_NOT_FOUND = "not found in cart"      # the one message _cart_action_any reacts to


def _cart_line_action(cookies: dict[str, str], edits: dict[str, Any],
                      merged: Optional[dict] = None, business: str = "") -> tuple[list[str], dict[str, str]]:
    """
    Fire one action per cart line against the merged snapshot (`merged`, read
    when not given). `edits`: cart_id → edit(fields), which sets operationType
    and the changed field on a copy of the line's block. Each response is
    folded back in, as the web page does.
    """
    if merged is None:
        merged, _ = _cart_pages(cookies, business)
    done: list[str] = []
    failed: dict[str, str] = {}
    for cid, edit in edits.items():
        cid = str(cid).strip()
        bid = cartlib.item_blocks(merged).get(cid)
        if bid is None:
            failed[cid] = LINE_NOT_FOUND
            continue
        block = json.loads(json.dumps(merged["data"][bid]))
        edit(block.setdefault("fields", {}))
        try:
            resp = _cart_async(merged, bid, block, cookies, business=business)
        except Exception as e:
            failed[cid] = str(e)
            continue
        response = (resp.get("global") or {}).get("response") or {}
        if response.get("success") is False:
            failed[cid] = response.get("tip") or "rejected"
            continue
        merged = cartlib.merge(merged, resp)
        if block["fields"].get("operationType") == "delete":
            merged["data"].pop(bid, None)
            for children in (merged.get("page") or {}).get("structure", {}).values():
                if bid in children:
                    children.remove(bid)
        done.append(cid)
    return done, failed


def _read_cart(cookies: dict[str, str], business: str = "") -> dict:
    return cartlib.parse(_cart_pages(cookies, business)[1])


def _render_order(cookies: dict[str, str], cart_ids: list[str]) -> dict:
    """The checkout page for these cart lines (renderOrder; places no order)."""
    payload = {
        "ultronVersion": "3.0", "_saasRegion": "aeg", "pageType": "Shipping",
        "_currency": CURRENCY, "locale": "en_US",
        "extraParam": json.dumps({"locale": "en_US", "intentional_currency": CURRENCY, "buyerCountry": COUNTRY,
                                  "platformType": "PC", "siteType": "", "af": "", "sk": ""}),
        "shippingAddressId": "0",
        "buyParams": json.dumps({"orderFrom": "SELECTIVE_CARTS", "items": [{"cartId": str(c)} for c in cart_ids]}),
        "contextID": f"PlaceOrder_{uuid.uuid4()}",
    }
    _cart_pace()
    resp = mtop_call("mtop.aliexpress.checkout.renderOrder", "1.0", payload, cookies=cookies,
                     method="POST", referer=f"{BASE_URL}/p/trade/confirm.html")
    _cart_check(resp)
    return checkoutlib.parse(resp)


def _checkout_preview_text(cookies: dict[str, str], cart_ids: list[str]) -> str:
    p = _render_order(cookies, cart_ids)
    return checkoutlib.format_preview(p, len(cart_ids), cartlib.checkout_url(cart_ids), DUTY_PER_LINE)


@tool()
def checkout_preview(cart_ids: Optional[list[str]] = None, list_name: str = "") -> str:
    """
    What checkout would really charge — read from AliExpress's own checkout
    page, nothing is bought: goods, discounts, shipping, import charges,
    total, the slowest line and the checkout link.

    Which lines: `cart_ids`, else the lines of `list_name`, else the only
    list there is, else the lines checked on the cart page.

    Args:
        cart_ids: Cart lines to price.
        list_name: A shopping list (see shop_list / set_cart_list).
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG
    try:
        ids = [str(c) for c in cart_ids or []]
        if not ids:
            cart = _read_cart(cookies)
            names = sorted({v.get("list") for v in _line_store().values() if v.get("list")})
            name = list_name or (names[0] if len(names) == 1 else "")
            ids = ([ln["cart_id"] for ln in cartlib.by_list(cart, _line_store())[0].get(name, [])] if name
                   else [ln["cart_id"] for ln in cart["lines"] if ln["checked"]])
            if not ids:
                return f"No lines for list '{name}'." if name else "No cart ids given and nothing is checked."
        return _checkout_preview_text(cookies, ids)
    except PermissionError:
        return AUTH_EXPIRED_MSG
    except Exception as e:
        return f"Checkout preview failed: {e}"


@tool()
def set_cart_list(cart_ids: list[str], list_name: str, need: str = "", query: str = "", match: str = "",
                  avoid: str = "", optional: Optional[bool] = None) -> str:
    """
    Put cart lines on a shopping list (or take them off with list_name="").
    The list decides how view_cart groups the cart and what checkout_preview
    prices. Lines the user added themselves stay theirs: tagging never makes
    them deletable by a plan.

    query / match / avoid tell view_cart how to search a Choice alternative
    for a line from a global seller (as in shop_list): without them it
    searches the need, which can find the wrong article (bare chip instead of
    a board).

    Args:
        cart_ids: Cart lines to tag.
        list_name: The list ("Sprach-Satellit"); empty removes the tag.
        need: Short name of what the line is for, in the user's language
            ("I2S-Mikrofon"); it is the line's name on the overview page.
        query: Search words for an alternative.
        match: Words the alternative's variant or title must contain.
        avoid: Words that rule an alternative out.
        optional: True for parts that are only "to compare" or nice to have;
            the overview offers to drop them.
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG
    try:
        lines = {ln["cart_id"]: ln for ln in _read_cart(cookies)["lines"]}
    except Exception as e:
        return f"Cart MTOP call failed: {e}"
    store = _line_store()
    tags, missing = {}, []
    for cid in [str(c) for c in cart_ids]:
        ln = lines.get(cid)
        if not ln:
            missing.append(cid)
            continue
        if not list_name:
            if store.get(cid, {}).get("own"):
                tags[cid] = None
            else:
                tags[cid] = {"list": "", "need": ""}
            continue
        tag = {"list": list_name.strip(), "need": need or store.get(cid, {}).get("need") or ""}
        for k, v in (("query", query), ("match", match), ("avoid", avoid)):
            if v:
                tag[k] = v
        if optional is not None:
            tag["optional"] = bool(optional)
        if cid not in store:
            tag.update(item_id=ln["item_id"], sku_id=ln["sku_id"], own=True)
        tags[cid] = tag
    _remember_lines({c: t for c, t in tags.items() if t}, drop=[c for c, t in tags.items() if t is None])
    out = f"{len(tags)} line(s) " + (f"on list '{list_name}'." if list_name else "taken off their list.")
    if missing:
        out += " Not in the cart: " + ", ".join(missing)
    return out


@tool(slow=True)
def recheck_cart(list_name: str = "", rank_top: int = 8, max_lines: int = 8, skip: int = 0,
                 job_id: str = "") -> str:
    """
    Is there anything cheaper than what is in the cart? For each cart line that
    belongs to a shopping list (tags from shop_list/set_cart_list), searches
    its need, prices the top hits (Choice first) for the same number of
    pieces like search_products(pieces=N) and returns a SWAP LIST: line,
    cheaper offer, saving in net EUR (shipping, import, discounts included),
    biggest saving first, plus the lines that are already cheapest.
    ANY saving is a reason to swap, also 0.10 EUR: no threshold. Only a bait
    variant or a wrong article (get_variant_images on the pick) rules an offer out.
    Read-only; slow. It runs as a background job: the call waits up to a minute; if the
    check is not finished it answers with progress and a job_id: call recheck_cart(job_id=...) again
    later (other tools stay usable meanwhile). Lines without match words are only reported as UNSURE.
    Slow because (product pages are cached for hours, the server paces its
    calls): work through a big cart with max_lines/skip and run it again with
    skip=<next_skip> until it says all lines are done.

    Args:
        list_name: only lines of this shopping list (empty: all listed lines).
        rank_top: hits priced per line (max 25).
        max_lines: lines checked per call.
        skip: lines to skip first (continue where the last call stopped).
        job_id: pick up a running or finished check (see below).
    """
    return _job_result(job_id, "recheck", lambda progress: _recheck_run(list_name, rank_top, max_lines, skip, progress))


def _recheck_run(list_name: str, rank_top: int, max_lines: int, skip: int, progress) -> str:
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG
    try:
        c = _read_cart(cookies)
    except Exception as e:
        return f"Cart MTOP call failed: {e}"
    tags = _line_store()
    skip = max(0, skip)
    todo = [ln for ln in c["lines"] if (tags.get(ln["cart_id"]) or {}).get("list")      # only lines that belong to a list
            and (not list_name or tags[ln["cart_id"]]["list"] == list_name)]
    if not todo:
        return "No cart line belongs to a shopping list" + (f" '{list_name}'" if list_name else "") + "; nothing to recheck."
    chunk = todo[skip: skip + max(1, max_lines)]
    swaps, same, failed, unsure = [], [], [], []
    for n_done, ln in enumerate(chunk):
        progress(n_done, len(chunk), (tags[ln["cart_id"]].get("need") or ln["title"])[:40])
        t = tags[ln["cart_id"]]
        pieces = max(1, ln["qty"] * deals.pack_size(ln["variant"]))
        query = t.get("query") or t.get("need") or ln["title"][:60]
        name = t.get("need") or ln["title"][:50]
        try:
            products = _search(query)
            rows, _, _ = _price_hits(products, pieces, t.get("match", ""), t.get("avoid", ""), rank_top, False)
            cur = _current_way(ln, pieces)
        except PermissionError:
            return AUTH_EXPIRED_MSG
        except Exception as e:
            failed.append(f"{name} ({ln['cart_id']}): {e}")
            continue
        gain, best = _saving(rows, cur)
        if best and not t.get("match"):
            # Without match words nothing says the offer is the same article (a 120 W charging station is no 30 W PSU).
            w, h = best
            unsure.append(f"- {name} (cart_id {ln['cart_id']}): cheapest hit {h['title'][:70]} ({h['item_id']}) would be {gain:.2f} "
                          f"{CURRENCY} less - UNVERIFIED, no match words on this line; not a swap until the article is checked")
        elif best:
            w, h = best
            swaps.append((gain, f"- {name} x{pieces} (cart_id {ln['cart_id']}): now {cur['net']:.2f} net -> {w['net']:.2f} net, "
                                f"SAVES {gain:.2f} {CURRENCY}"
                                + f"\n  {h['title'][:80]}" + (" - Choice" if w["offer"]["choice"] else "")
                                + f"\n  {_way_text(w)}\n  do: item {h['item_id']} "
                                + ", ".join(f"sku {v['sku_id']}x{q}" for v, q in w["lines"])
                                + f" (shop_list item_ids / add_to_cart), then remove_from_cart {ln['cart_id']}"))
        elif cur and not rows:
            failed.append(f"{name} ({ln['cart_id']}): no other offer could be priced (bot check or no hit with the match words)"
                          f" - search by hand; the line itself costs {cur['net']:.2f} net")
        elif cur:
            same.append(f"- {name} ({ln['cart_id']}): already cheapest of {len(rows)} priced ({cur['net']:.2f} net)")
        else:
            failed.append(f"{name} ({ln['cart_id']}): line could not be priced")
    out = [f"Recheck of {len(chunk)} of {len(todo)} listed lines (net incl. shipping, import, discounts):"]
    out.append(f"SWAP LIST ({len(swaps)}, total saving {sum(g for g, _ in swaps):.2f} {CURRENCY}) - swap all of them, any saving counts; "
               "check bait/wrong article via get_variant_images first:" if swaps else ("SWAP LIST: empty (lines under UNSURE are not verified swaps)." if unsure
                                 else "SWAP LIST: empty, every checked line is already the cheapest."))
    out += [t for _, t in sorted(swaps, key=lambda x: -x[0])]
    if unsure:
        out += ["UNSURE - NOT swaps (these lines have no match words; check that the offer is the same article, e.g. power, pole "
                "count, length, plug type, female/male; then set_cart_list(match=..., avoid=...) and recheck):"] + unsure
    if same:
        out += ["Already cheapest:"] + same
    if failed:
        out += ["Could not check (compare by hand):"] + failed
    nxt = skip + len(chunk)
    out.append(f"{len(todo) - nxt} lines left: run recheck_cart again with skip={nxt}." if nxt < len(todo) else "All listed lines are done.")
    return "\n".join(out)


def _kombi_orders(picks: list[dict], limit: int) -> list[list[dict]]:
    """Split the Kombi picks into orders of at most `limit` lines; each order needs 3+ pieces (a short last one joins the one before)."""
    orders: list[list[dict]] = []
    n_lines = lambda o: sum(p.get("lines", 1) for p in o)
    for p in sorted(picks, key=lambda x: -x["pieces"]):
        orders.append([p]) if not orders or n_lines(orders[-1]) + p.get("lines", 1) > limit else orders[-1].append(p)
    pcs = lambda o: sum(p["pieces"] for p in o)
    while len(orders) > 1 and min(map(pcs, orders)) < COMBO_MIN_PIECES:     # an order below 3 pieces saves nothing:
        orders.sort(key=pcs)                                                # join the two smallest (limit is soft then)
        small = orders.pop(0)
        orders[0] += small
    return orders


def _kombi_run(list_name: str, rank_top: int, max_lines: int, skip: int, progress) -> str:
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG
    try:
        c = _read_cart(cookies)
    except Exception as e:
        return f"Cart MTOP call failed: {e}"
    tags = _line_store()
    todo = [ln for ln in c["lines"] if (tags.get(ln["cart_id"]) or {}).get("list")
            and (not list_name or tags[ln["cart_id"]]["list"] == list_name)]
    if not todo:
        return "No cart line belongs to a shopping list" + (f" '{list_name}'" if list_name else "") + "; nothing to compare."
    chunk = todo[max(0, skip): max(0, skip) + max(1, max_lines)]
    moves, none, unsure, failed = [], [], [], []
    for n_done, ln in enumerate(chunk):
        t = tags[ln["cart_id"]]
        name = t.get("need") or ln["title"][:50]
        progress(n_done, len(chunk), name[:40])
        pieces = max(1, ln["qty"] * deals.pack_size(ln["variant"]))
        query = t.get("query") or t.get("need") or ln["title"][:60]
        try:
            hits = [h for h in _search(query) if h.get("combo")]
            if not hits:
                none.append(f"- {name} ({ln['cart_id']}): no Kombi-Knaller offer in the hits of '{query}'")
                continue
            rows, _, _ = _price_hits(hits, pieces, t.get("match", ""), t.get("avoid", ""), rank_top, False)
            cur = _current_way(ln, pieces)
        except PermissionError:
            return AUTH_EXPIRED_MSG
        except Exception as e:
            failed.append(f"{name} ({ln['cart_id']}): {e}")
            continue
        rows = [(w, h) for w, h in rows if w["pieces"] >= pieces]
        if not cur or not rows:
            failed.append(f"{name} ({ln['cart_id']}): Kombi offers or the line could not be priced")
            continue
        # With 3+ pieces in the combo order the import charge of a Kombi line is gone.
        best = min(rows, key=lambda r: r[0]["net"] - (0 if r[0].get("combo_free") else r[0].get("duty_back", 0.0)))
        w, h = best
        kombi_net = w["net"] - (0 if w.get("combo_free") else w.get("duty_back", 0.0))
        gain = cur["net"] - kombi_net
        entry = {"name": name, "cart_id": ln["cart_id"], "pieces": w["pieces"], "gain": gain, "kombi_net": kombi_net,
                 "now": cur["net"], "item_id": h["item_id"], "title": h["title"][:70], "way": w,
                 "do": ", ".join(f"sku {v['sku_id']}x{q}" for v, q in w["lines"])}
        if gain <= 0.004:
            none.append(f"- {name} ({ln['cart_id']}): Kombi is not cheaper ({kombi_net:.2f} vs {cur['net']:.2f} net)")
        elif not t.get("match"):
            unsure.append(f"- {name} ({ln['cart_id']}): Kombi {h['title'][:60]} ({h['item_id']}) would save {gain:.2f} - UNVERIFIED, "
                          "no match words on this line; check that it is the same article first")
        else:
            moves.append(entry)
    out = [f"Kombi-Knaller check of {len(chunk)} of {len(todo)} listed lines (net incl. shipping, discounts; Kombi without "
           "import charge, which needs 3+ Kombi pieces in the order):"]
    if moves:
        pcs = sum(m_["pieces"] for m_ in moves)
        out.append(f"KOMBI IS CHEAPER for {len(moves)} line(s), total {sum(m_['gain'] for m_ in moves):.2f} {CURRENCY}, {pcs} Kombi pieces:")
        for m_ in sorted(moves, key=lambda x: -x["gain"]):
            out.append(f"- {m_['name']} x{m_['pieces']} (cart_id {m_['cart_id']}): now {m_['now']:.2f} -> Kombi {m_['kombi_net']:.2f}, "
                       f"SAVES {m_['gain']:.2f}\n  {m_['title']}\n  do: add_to_cart item {m_['item_id']} {m_['do']} combo=True, "
                       f"then remove_from_cart {m_['cart_id']}")
        limit = COMBO_MAX_LINES or 10
        orders = _kombi_orders(moves, limit) if pcs >= COMBO_MIN_PIECES else []
        if not orders:
            out.append(f"Only {pcs} Kombi piece(s): below {COMBO_MIN_PIECES}, so no import charge is saved. Leave it.")
        else:
            out.append(f"Orders ({len(orders)}; up to {limit} lines each"
                       + ("" if COMBO_MAX_LINES else ", the real limit is not measured: if the checkout says 'Limit exceeded' use more rounds")
                       + "):")
            for i, o in enumerate(orders, 1):
                out.append(f"  Order {i}: " + ", ".join(f"{x['name']} x{x['pieces']}" for x in o)
                           + f" = {sum(x['pieces'] for x in o)} pcs")
            out.append("Everything else stays in the main cart (its import charges drop with each line that moves).")
    else:
        out.append("No line is cheaper as Kombi.")
    if unsure:
        out += ["UNSURE - not moves:"] + unsure
    if none:
        out += ["No Kombi advantage:"] + none
    if failed:
        out += ["Could not check:"] + failed
    nxt = max(0, skip) + len(chunk)
    out.append(f"{len(todo) - nxt} lines left: run kombi_check again with skip={nxt}." if nxt < len(todo) else "All listed lines are done.")
    return "\n".join(out)


@tool(slow=True)
def kombi_check(list_name: str = "", rank_top: int = 6, max_lines: int = 12, skip: int = 0, job_id: str = "") -> str:
    """
    Where would the Kombi-Knaller (Bundle Deals) be cheaper? For each cart line of
    a shopping list: searches its need, takes only the hits flagged Kombi, prices them
    for the same number of pieces WITHOUT import charge (3+ Kombi pieces in the
    combo order) and compares with the line as it stands (shipping, import,
    discounts). Result: the lines where Kombi wins, with the saving, the add/remove
    steps, and the Kombi lines spread over orders (the combo checkout has a line
    limit; several orders cost nothing extra). Lines without match words are only
    UNSURE. Read-only. Cheap: one search per line (cached 30 min) plus the few
    Kombi hits. Runs as a background job like recheck_cart: after a minute it answers
    with progress and a job_id; call again with job_id to pick it up.

    Args:
        list_name: only lines of this shopping list (empty: all listed lines).
        rank_top: Kombi hits priced per line (max 25).
        max_lines: lines per call; continue with skip.
        skip: lines to skip first.
        job_id: pick up a running or finished check.
    """
    return _job_result(job_id, "kombi", lambda progress: _kombi_run(list_name, rank_top, max_lines, skip, progress))


def _plan_store() -> dict:
    try:
        return json.loads(PLANS_FILE.read_text())
    except (OSError, ValueError):
        return {}


@tool()
def combo_round(plan_id: str, round_no: int = 0, add: bool = True) -> str:
    """
    The next Kombi-Knaller order of a shop_list plan. The combo checkout takes only so many
    lines, so shop_list spreads the Kombi lines over rounds and stores them under a plan id;
    apply puts round 1 into the combo cart. After you have ordered it, call this again: it puts
    the next round (or `round_no`) into the combo cart and checks ONLY those lines (it UNCHECKS every other Kombi line, also ones of other lists: the checkout
    page then shows exactly this order; the result names what it unchecked). Never orders anything.

    Args:
        plan_id: the id shop_list printed ("plan-ab12cd").
        round_no: which round (1, 2, ...); 0 = the first round not put in yet.
        add: False only shows the state of every round.
    """
    store = _plan_store()
    plan = store.get(plan_id)
    if not plan:
        return f"No plan '{plan_id}' (ids come from shop_list; the last 20 are kept)."
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG
    try:
        cart = _read_cart(cookies, COMBO)["lines"]
    except Exception as e:
        return f"Kombi-Knaller cart not readable: {e}"
    have = {(ln["item_id"], ln["sku_id"]): ln for ln in cart if ln["valid"]}

    def state(r: dict) -> str:
        return _round_state(r, have)
    rounds = plan["rounds"]
    out = [f"Plan {plan_id} ({plan['list'] or 'no list'}): {len(rounds)} Kombi round(s)"]
    for i, r in enumerate(rounds, 1):
        out.append(f"  Round {i}: " + ", ".join(f"{p['need']} x{p['pieces']}" for p in r["picks"]) + f" - {state(r)}")
    if not add:
        return "\n".join(out)
    with _STATE_LOCK:                       # decide and claim in one step: no second call starts the same round
        cur = _plan_store()
        rounds = (cur.get(plan_id) or {}).get("rounds") or rounds
        n = round_no or next((i for i, r in enumerate(rounds, 1) if not r["added"]), 0)
        if not n or n > len(rounds):
            return "\n".join(out + ["Every round has been put in. Nothing left."])
        if not round_no and plan_id in cur:
            cur[plan_id]["rounds"][n - 1]["added"] = True
            private.write_json(PLANS_FILE, cur)
    r = rounds[n - 1]
    kombi = {(p["item_id"], ln["sku_id"]): {"need": p["need"], "label": ln["label"], "qty": ln["qty"], "price": ln["price"]}
             for p in r["picks"] for ln in p["lines"]}
    log = _apply_combo(cookies, plan["list"], kombi)
    try:
        now = {(ln["item_id"], ln["sku_id"]): ln["cart_id"] for ln in _read_cart(cookies, COMBO)["lines"] if ln["valid"]}
        ids = [now[k] for k in kombi if k in now]
        text, _ = _select(cookies, ids, only=True, business=COMBO)
    except Exception as e:
        return "\n".join(out + [f"Round {n}:"] + [f"- {x}" for x in log] + [f"Checking the round failed: {e}"])
    with _STATE_LOCK:
        cur = _plan_store()
        if plan_id in cur:
            cur[plan_id]["rounds"][n - 1]["added"] = not any(x.startswith("FAILED") for x in log)
            private.write_json(PLANS_FILE, cur)
    return "\n".join(out + [f"Round {n} put in:"] + [f"- {x}" for x in log] + ["", text])


_JOBS: dict[str, dict] = {}
JOB_WAIT_S = float(os.environ.get("ALIEXPRESS_JOB_WAIT", "60"))


def _job_result(job_id: str, kind: str, work) -> str:
    """Run `work(progress)` as a background job (or pick one up by id); wait up to JOB_WAIT_S for it."""
    if job_id:
        job = _JOBS.get(job_id)
        if not job:
            return f"No job '{job_id}' (the server restarted, or it was never started)."
    else:
        for jid in [k for k, j in _JOBS.items() if j["done"] and time.time() - j["t0"] > 3600][:50]:
            _JOBS.pop(jid, None)
        job_id = f"{kind}-{uuid.uuid4().hex[:6]}"
        job = {"id": job_id, "t0": time.time(), "done": False, "result": None, "progress": "starting"}
        _JOBS[job_id] = job

        def progress(i, n, what):
            job["progress"] = f"{i} of {n} lines done, now: {what}"

        def run():
            _CALL.calls, _CALL.waited, _CALL.background = 0, 0.0, True
            try:
                job["result"] = work(progress)
            except Exception as e:
                job["result"] = f"Job failed: {e}"
            job["calls"], job["waited"] = _CALL.calls, _CALL.waited
            job["done"], job["took"] = True, time.time() - job["t0"]

        threading.Thread(target=run, daemon=True, name=job_id).start()
    deadline = time.time() + JOB_WAIT_S
    while not job["done"] and time.time() < deadline:
        time.sleep(0.5)
    if not job["done"]:
        return (f"Still running (job {job_id}, {time.time() - job['t0']:.0f} s so far): {job['progress']}. "
                f"Call again with job_id='{job_id}' to wait for the result; other tools work meanwhile.")
    return (job["result"] + f"\n\n(job {job_id}: {job['took']:.0f} s, {job.get('calls', 0)} AliExpress call(s), "
            f"{job.get('waited', 0):.0f} s of it waiting for the pacing gap between calls)")


def _combo_channel(cart: dict) -> Optional[dict]:
    return next((x for x in cart["header"].get("channels") or [] if x.get("business") == COMBO and x.get("count")), None)


def _combo_lines(cookies: dict[str, str], cart: dict) -> Optional[list[dict]]:
    """The lines of the Kombi-Knaller cart; None when there is none, [] when it could not be read."""
    if not _combo_channel(cart):
        return None
    try:
        return _read_cart(cookies, COMBO)["lines"]
    except Exception as e:
        logger.debug("combo cart not readable: %s", e)
        return []


def _round_state(r: dict, have: dict) -> str:
    keys = [(p["item_id"], ln["sku_id"]) for p in r["picks"] for ln in p["lines"]]
    n = sum(1 for k in keys if k in have)
    if n == len(keys):
        return "in the combo cart"
    if n:
        return f"partly in the combo cart ({n} of {len(keys)} lines)"
    return "gone from the cart (ordered or removed)" if r["added"] else "not put in yet"


def _combo_page(cart: dict, lines: Optional[list[dict]], tags: dict, names: list[str]) -> Optional[dict]:
    """The Kombi-Knaller basket as the cart page shows it: rows, checked pieces, rounds of the stored plans."""
    ch = _combo_channel(cart)
    if not ch or not lines:
        return None
    ok = [ln for ln in lines if ln["valid"]]
    rows = [{"img": ln.get("image"), "need": (tags.get(ln["cart_id"]) or {}).get("need") or ln["title"][:40],
             "title": ln["title"], "url": f"{BASE_URL}/item/{ln['item_id']}.html", "qty": ln["qty"],
             "variant": ln["variant"] or "Standard", "cost": (ln["price"] or 0) * ln["qty"], "checked": ln["checked"]}
            for ln in ok]
    pick = [ln for ln in ok if ln["checked"]]
    have = {(ln["item_id"], ln["sku_id"]): ln for ln in ok}
    rounds = []
    store = _plan_store()
    for pid in sorted(store, key=lambda k: store[k]["at"], reverse=True):
        if store[pid]["list"] in names and len(store[pid]["rounds"]) > 0:
            rounds = [{"n": i, "names": ", ".join(f"{p['need']} x{p['pieces']}" for p in rd["picks"]),
                       "state": _round_state(rd, have)} for i, rd in enumerate(store[pid]["rounds"], 1)]
            break
    return {"rows": rows, "pieces": sum(ln["qty"] for ln in pick), "goods": sum((ln["price"] or 0) * ln["qty"] for ln in pick),
            "url": ch.get("url"), "rounds": rounds}


def _combo_text(cookies: dict[str, str], cart: dict, lines: Optional[list[dict]] = None) -> str:
    """The Kombi-Knaller cart (channel cart nn_mix): its lines, what is checked, and whether the import charge is covered."""
    ch = _combo_channel(cart)
    if not ch:
        return ""
    if lines is None:
        lines = _combo_lines(cookies, cart)
    if not lines:
        return f"## Kombi-Knaller cart ({ch['count']} items): not readable"
    ok = [ln for ln in lines if ln["valid"]]
    pick = [ln for ln in ok if ln["checked"]]
    pieces = sum(ln["qty"] for ln in pick)
    total = sum((ln["price"] or 0) * ln["qty"] for ln in pick)
    out = [f"## Kombi-Knaller cart ({COMBO}): {len(ok)} valid line(s) of {len(lines)}, {len(pick)} checked = {pieces} pcs, "
           f"{total:.2f} {CURRENCY} goods. " + ("3+ pieces checked: import charge covered." if pieces >= 3
                                                else "Fewer than 3 pieces checked: the import charge is NOT covered.")
           + f" Order it on its page: {ch.get('url') or ''} (Limit exceeded = remove lines or order in several rounds)."]
    out += [f"- cart_id {ln['cart_id']}: {ln['qty']}× {ln['variant'][:30]} — {ln['title'][:50]}, {ln['price']} each, "
            + ("checked" if ln["checked"] else "unchecked") + ("" if ln["valid"] else ", INVALID") for ln in lines]
    return "\n".join(out)


@tool()
def view_cart(checked_only: bool = False, checkout: bool = True, summary: str = "",
              options: Optional[list[dict]] = None, alternatives: bool = True) -> str:
    """
    The cart, read-only (it does not look for cheaper offers: recheck_cart does;
    any saving, also 0.10 EUR, is a reason to switch): each shopping list with its lines, its checkout
    total and checkout link, leftovers flagged; then the other lines and a
    shipping analysis. Writes the overview page (cart.html) with the
    decisions (global lines → Choice offers, optional parts, leftovers).

    Args:
        checked_only: list only the ticked lines among the others.
        checkout: price every list at checkout (one call per list).
        summary: 1–3 sentences for the top of the page: what you recommend
            and why, in the user's language.
        options: extra decisions for the page, each {title, text, delta
            (EUR, + dearer / − cheaper), link, img, do}.
        alternatives: search Choice offers for global seller lines (cached
            6 h; the first call takes a while).
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG
    try:
        c = _read_cart(cookies)
    except Exception as e:
        return f"Cart MTOP call failed: {e}"
    combo_lines = _combo_lines(cookies, c)
    if not c["lines"]:
        combo = _combo_text(cookies, c, combo_lines)      # the main cart may be empty while the Kombi-Knaller cart is not
        return combo or f"The cart is empty (server count: {c['header'].get('count')})."
    tags = _line_store()
    text = cartlib.format_cart(c, show_unchecked=not checked_only, tags=tags)
    priced: dict[str, dict] = {}
    previews = []
    for name, lines in cartlib.by_list(c, tags)[0].items() if checkout else []:
        ids = [ln["cart_id"] for ln in lines]
        try:
            priced[name] = _render_order(cookies, ids)
            previews.append(f"## Checkout for list '{name}'\n"
                            + checkoutlib.format_preview(priced[name], len(ids), cartlib.checkout_url(ids), DUTY_PER_LINE))
        except Exception as e:
            previews.append(f"## Checkout for list '{name}': preview failed ({e})")
    n_lists = len(cartlib.by_list(c, tags)[0])
    combined = next(iter(priced.values())) if n_lists == 1 and priced else None
    all_ids = [ln["cart_id"] for lns in cartlib.by_list(c, tags)[0].values() for ln in lns]
    if checkout and n_lists > 1 and all_ids:
        try:
            combined = _render_order(cookies, all_ids)
        except Exception as e:
            previews.append(f"## Combined checkout: preview failed ({e})")
    try:
        page, dec = _cart_report(c, tags, priced, summary, options, alternatives, combined, combo_lines)
        real = [d for d in dec if report._is_decision(d)]
        if real:
            page += ("\n\nDecisions on the page, numbered as there. The page has no buttons: add ONE `decide add` card per "
                     "decision (--option per choice, --recommend, effect in euro and days), then tell the user "
                     "\"Die Fragen liegen als Karten bei dir\". What to do per answer:\n" + "\n".join(
                f"{i}. {d['title']}: " + (f"{d['price'][0]:+.2f}" + (f"…{d['price'][1]:+.2f}" if abs(d['price'][1] - d['price'][0]) >= 0.005 else "") + " EUR"
                   if d.get("price") else "no price change")
                + (f", delivery {d['date'][0]} → {d['date'][1]}" if d.get("date") else "")
                + (f" — do: {d['do']}" if d.get("do") else "") for i, d in enumerate(real, 1)))
    except Exception as e:
        page = f"(page not written: {e})"
    return "\n\n".join(["## For the user — say this in a few sentences\n" + _chat_cart(c, tags, priced, combined),
                         page, "## Details (for you)\n" + text, _combo_text(cookies, c, combo_lines)] + previews).replace("\n\n\n\n", "\n\n")


@tool()
def select_cart_items(cart_ids: list[str], only: bool = False, selected: bool = True) -> str:
    """
    Check (or uncheck) cart lines for checkout, and return the checkout link
    for exactly these lines. This changes the real cart's selection, not its
    content.

    Every given line is checked; the cart is read again afterwards and any line
    that is still not checked is named ("NOT checked"), so nothing is lost
    silently. The returned checkout link holds every given line: it opens the
    order confirmation with exactly those lines, where the user decides to pay.

    Args:
        cart_ids: cart_id values from view_cart.
        only: uncheck every other line first, so the cart shows exactly these.
        selected: False unchecks the given lines instead.

    A cart_id that is not in the main cart is looked up in the Kombi-Knaller
    cart (view_cart lists both); `only` then applies to each cart on its own.
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG
    try:
        ids = [str(c).strip() for c in cart_ids if str(c).strip()]
        lost: list[str] = []
        text = _select(cookies, ids, only=only, selected=selected, unknown_out=lost)[0]
        if lost:
            text += "\n\nKombi-Knaller cart (ids not in the main cart):\n" + _select(
                cookies, lost, only=only, selected=selected, business=COMBO)[0]
        return text
    except Exception as e:
        return f"Cart call failed: {e}"


def _select(cookies: dict[str, str], want: list[str], only: bool = False,
            selected: bool = True, business: str = "", unknown_out: Optional[list] = None) -> tuple[str, dict]:
    """Check / uncheck lines in one pass; returns the report and the cart afterwards.
    `unknown_out` collects the ids not in this cart. `business`: COMBO for the Kombi-Knaller cart (own page, no 10-line limit known, no checkout link)."""
    merged, pages = _cart_pages(cookies, business)
    before = cartlib.parse(pages)
    titles = {ln["cart_id"]: f"{ln['title'][:40]} ({ln['variant']})" for ln in before["lines"]}
    state_of = {ln["cart_id"]: ln["checked"] for ln in before["lines"]}
    unknown = [c for c in want if c not in state_of]
    want = [c for c in want if c in state_of]
    out = []
    if not selected:
        done, failed = _cart_line_action(cookies, {c: _edit_check(False) for c in want if state_of[c]}, merged, business)
        out.append(f"Unchecked {len(done)} line(s).")
    else:
        # Uncheck first, then check every wanted line (no cap: a guessed 10-line limit used to drop the rest).
        off = [c for c, on in state_of.items() if on and c not in want] if only else []
        on = [c for c in want if not state_of[c]]
        done, failed = _cart_line_action(
            cookies, {**{c: _edit_check(False) for c in off}, **{c: _edit_check(True) for c in on}}, merged, business)
        out.append(f"Checked {sum(1 for c in on if c in done)} line(s); {len(want) - len(on)} were already checked.")
        if any(c in done for c in off):
            out.append("Unchecked (not in the list): " + "; ".join(titles[c] for c in off if c in done))
    for c, why in failed.items():
        out.append(f"Failed {titles.get(c, c)}: {why}")
    if unknown_out is not None:
        unknown_out.extend(unknown)
    elif unknown:
        out.append("Not in cart: " + ", ".join(unknown))
    after = _read_cart(cookies, business)
    s = after["summary"]
    # Verify against the cart as it is now, not against the answers of the single calls.
    now = {ln["cart_id"]: ln["checked"] for ln in after["lines"]}
    wrong = [c for c in want if c not in failed and now.get(c) is not selected]
    if wrong:
        out.append(("NOT checked after the call" if selected else "STILL checked after the call")
                   + f" ({len(wrong)}): " + "; ".join(titles.get(c, c) for c in wrong))
    if business:
        pick = [ln for ln in after["lines"] if ln["valid"] and ln["checked"]]
        pieces = sum(ln["qty"] for ln in pick)
        out.append(f"Kombi-Knaller cart now: {len(pick)} line(s) checked = {pieces} pcs, goods "
                   f"{sum((ln['price'] or 0) * ln['qty'] for ln in pick):.2f} {CURRENCY}; "
                   + ("3+ pieces: no import charge." if pieces >= 3 else "fewer than 3 pieces: the import charge is NOT covered.")
                   + " It is ordered on its own page (view_cart shows the link).")
    elif s.get("total") is not None:
        out.append(f"Cart page now: {sum(1 for ln in after['lines'] if ln['checked'])} line(s) checked, "
                   f"goods {s.get('subtotal') or 0:.2f} + shipping {s.get('shipping') or 0:.2f} = "
                   f"{s['total']:.2f} {s.get('currency') or CURRENCY}")
    if selected and want and not business:
        out.append(f"Checkout link ({len(want)} line(s)): {cartlib.checkout_url(want)}")
    return "\n".join(out), after


def _cart_action_any(cookies: dict[str, str], edits: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """Run line actions on the main cart; lines it does not have are tried in the Kombi-Knaller cart."""
    done, failed = _cart_line_action(cookies, edits)
    lost = {c: e for c, e in edits.items() if failed.get(c) == LINE_NOT_FOUND}
    if lost:
        d2, f2 = _cart_line_action(cookies, lost, business=COMBO)
        failed = {**{c: w for c, w in failed.items() if c not in lost}, **f2}
        done = done + d2
    return done, failed



# Cart lines the MCP knows about: {cart_id: {item_id, sku_id, added, list,
# need, own}}. Lines it put into the cart itself may be removed by a plan;
# lines marked `own` are the user's (only tagged with a list, never deleted).
# `list` / `need` say which shopping list and which need a line is for, so the
# cart can be shown per list instead of as one heap.
MCP_LINES_FILE = Path(os.environ.get(
    "ALIEXPRESS_MCP_LINES", str(CREDENTIALS_PATH.parent / "aliexpress-mcp-lines.json")))


def _line_store() -> dict[str, dict]:
    try:
        return json.loads(MCP_LINES_FILE.read_text())
    except (OSError, ValueError):
        return {}


_STATE_LOCK = threading.RLock()     # read-modify-write of the small state files (tools run in threads)


def _remember_lines(add: dict[str, dict] | None = None, drop: list[str] | None = None) -> None:
    with _STATE_LOCK:
        lines = _line_store()
        for cid, info in (add or {}).items():
            lines[str(cid)] = {**lines.get(str(cid), {}), **info}
        for cid in drop or []:
            lines.pop(str(cid), None)
        private.write_json(MCP_LINES_FILE, lines)


def _cart_add(cookies: dict[str, str], item_id: str, sku_id: str, quantity: int,
              known: Optional[set[str]] = None, business: str = "") -> str:
    """
    Add one variant; returns its cart_id. Raises on refusal. Adding a variant
    that is already in the cart raises that line's quantity and returns its
    id: such a line stays the user's and is not remembered as the MCP's.
    `known`: cart ids before the add (read from the cart when not given).
    """
    if known is None:
        known = {ln["cart_id"] for ln in _read_cart(cookies, business)["lines"]}
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
    if business:   # what the Kombi-Knaller page sends: the channel cart, not the main one
        payload["business"] = business
        payload["bizParams"] = json.dumps({"platformType": "DESKTOP", "isNewStyle": True, "useLimitStyle": True,
                                           "sourceBizPage": "nnHsCode"})
    _cart_pace()
    resp = mtop_call(
        "mtop.aliexpress.trade.cart.add", "1.0", payload,
        cookies=cookies, referer=f"{BASE_URL}/item/{item_id}.html", method="POST",
    )
    _cart_check(resp)
    data = resp.get("data") or {}
    if data.get("addFailed"):
        raise RuntimeError(data.get("errorMessage") or data.get("failReason") or "addFailed")
    if not data.get("cartId"):
        raise RuntimeError("AliExpress returned no cart id")
    cid = str(data["cartId"])
    if cid not in known:
        _remember_lines({cid: {"item_id": str(item_id), "sku_id": str(sku_id), "added": time.strftime("%Y-%m-%d")}})
    return cid


@tool()
def add_to_cart(item_id: str, sku_id: str, quantity: int = 1, list_name: str = "", need: str = "",
                combo: bool = False) -> str:
    """
    Add a product variant to the AliExpress cart and check it for checkout (the
    Kombi-Knaller cart ticks new lines itself).
    This changes the real cart (it does not buy anything). Adding a variant
    that is already in the cart raises that line's quantity instead.
    Every cart line pays an import charge: prefer a bigger pack in one line.

    Args:
        item_id: AliExpress item ID.
        sku_id: Variant from get_product_variants (required; picks pack size/color).
        quantity: How many of that variant.
        list_name: Shopping list the line belongs to (see set_cart_list).
        need: What it is for on that list ("I2S microphone").
        combo: Put it into the Kombi-Knaller cart (only for hits flagged "Kombi"
            in search_products): 3+ pieces there are ordered without import
            charge. Adding a variant that is already there REPLACES that line
            (new cart_id): give the TOTAL quantity you want, not the extra.
    """
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG
    if quantity < 1:
        return "quantity must be at least 1."
    try:
        cid = _cart_add(cookies, item_id, sku_id, quantity, business=COMBO if combo else "")
    except Exception as e:
        return f"Add to cart failed: {e}"
    if combo:
        if list_name:
            # an add of a variant already there replaces its line under a new id: forget the old id
            old = [k for k, v in _line_store().items() if v.get("combo") and v.get("sku_id") == str(sku_id)
                   and v.get("item_id") == str(item_id) and k != cid]
            _remember_lines({cid: {"list": list_name.strip(), "need": need, "item_id": str(item_id), "sku_id": str(sku_id),
                                   "own": True, "combo": True}}, drop=old)
        try:
            line = next((ln for ln in _read_cart(cookies, COMBO)["lines"] if ln["cart_id"] == cid), None)
        except Exception as e:
            return f"Added {quantity} × item {item_id} to the Kombi-Knaller cart, cart_id {cid}, but reading it failed: {e}"
        return (f"Added to the Kombi-Knaller cart: {line['qty']} × {line['title'][:50]} ({line['variant']}), {line['price']} each, "
                + ("checked" if line["checked"] else "NOT checked") + f", cart_id {cid}. 3+ pieces there pay no import charge."
                if line else f"Added item {item_id} to the Kombi-Knaller cart, cart_id {cid} (view_cart shows it).")
    if list_name:
        tag = {"list": list_name.strip(), "need": need}
        if cid not in _line_store():
            tag.update(item_id=str(item_id), sku_id=str(sku_id), own=True)
        _remember_lines({cid: tag})
    dropped = []
    try:
        merged, pages = _cart_pages(cookies)
        c = cartlib.parse(pages)
        line = next((ln for ln in c["lines"] if ln["cart_id"] == cid), None)
        if line and not line["checked"]:
            before = {ln["cart_id"]: ln for ln in c["lines"] if ln["checked"]}
            _cart_line_action(cookies, {cid: _edit_check(True)}, merged)
            c = _read_cart(cookies)
            line = next((ln for ln in c["lines"] if ln["cart_id"] == cid), None)
            now = {ln["cart_id"] for ln in c["lines"] if ln["checked"]}
            dropped = [ln for k, ln in before.items() if k not in now]
    except Exception as e:
        return f"Added {quantity} × item {item_id} (sku {sku_id}), cart_id {cid}, but checking it failed: {e}"
    if not line:
        return (f"Added item {item_id} (sku {sku_id}), cart_id {cid}, but it is not in the main cart — "
                "AliExpress may have put it in a channel cart (see view_cart).")
    ship = "free" if line["ship"] == 0 else (f"{line['ship']:.2f}" if line["ship"] is not None else "?")
    price = f"{line['price']:.2f}" if line["price"] is not None else "?"
    group = cartlib.GROUP_NAMES.get(line["group"], line["group"])
    out = (
        f"Added and checked: {line['qty']} × {line['title'][:50]} ({line['variant']}), "
        f"{price} each, shipping {ship}, delivery {line['delivery'][0]}…{line['delivery'][1]}, "
        f"{group}. cart_id {cid}."
    )
    if dropped:
        out += (" AliExpress unchecked other lines when this one was added: "
                + "; ".join(f"{ln['title'][:40]} ({ln['variant']})" for ln in dropped)
                + ". Check them again with select_cart_items.")
    return out


@tool()
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

    try:
        done, failed = _cart_action_any(cookies, {str(cart_id).strip(): _edit_qty(quantity)})
    except Exception as e:
        return f"Cart call failed: {e}"
    if failed:
        return f"cart_id {cart_id}: {next(iter(failed.values()))}"
    return f"cart_id {cart_id}: quantity now {quantity}."


@tool()
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

    try:
        done, failed = _cart_action_any(cookies, {str(c).strip(): _edit_delete for c in cart_ids if str(c).strip()})
    except Exception as e:
        return f"Cart call failed: {e}"
    _remember_lines(drop=done)
    lines = [f"Removed {len(done)} item(s)."]
    if failed:
        lines.append("Failed: " + "; ".join(f"{c}: {m}" for c, m in failed.items()))
    return "\n".join(lines)


_REPORT_PDP_LOCK = threading.Lock()
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _public_https(url: str) -> bool:
    """True for an https URL whose host resolves only to public addresses (no loopback, LAN, metadata)."""
    import ipaddress
    import socket
    from urllib.parse import urlparse
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname:
        return False
    try:
        return all(ipaddress.ip_address(a[4][0]).is_global
                   for a in socket.getaddrinfo(u.hostname, u.port or 443, proto=socket.IPPROTO_TCP))
    except (OSError, ValueError):
        return False


def _cdn_picture_url(url: str) -> str:
    """AliExpress CDN picture at 800x800, leaving URLs that already carry a size or a query alone."""
    if "?" in url or re.search(r"_\d+x\d+\w*\.\w+$", url) or not url.lower().endswith((".jpg", ".jpeg")):
        return url
    return url + "_800x800.jpg"


def _compare_image(cart_id: str, left: Optional[str], right: Optional[str]) -> Optional[str]:
    """Original left, candidate right, as one picture in the report folder, so the agent can look at both (path, or None)."""
    try:
        from io import BytesIO
        from PIL import Image as PILImage
        ims = []
        for src in (left, right):
            if not src or not src.startswith("data:image/"):
                return None
            ims.append(PILImage.open(BytesIO(base64.b64decode(src.split(",", 1)[1]))).convert("RGB").resize((400, 400)))
        sheet = PILImage.new("RGB", (820, 400), (255, 255, 255))
        sheet.paste(ims[0], (0, 0))
        sheet.paste(ims[1], (420, 0))
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"vergleich-{re.sub(r'[^0-9A-Za-z]', '', str(cart_id))}.png"
        sheet.save(path)
        return str(path)
    except Exception as e:
        logger.debug("comparison picture failed: %s", e)
        return None


def _report_image(card: dict) -> Optional[str]:
    """data: URI for one report card: its `image` (https URL, file inside the report folder, data: URI), else the picture of `item_id`."""
    import mimetypes
    src = (card.get("image") or "").strip()
    if not src and card.get("item_id"):
        with _REPORT_PDP_LOCK:  # one product page at a time: shared pacing, cache and cookie refresh
            offer = _pdp_offer(str(card["item_id"]))
        if offer:
            sku = str(card.get("sku_id") or "")
            src = next((v.get("image") for v in offer["variants"]
                        if sku and str(v.get("sku_id")) == sku and v.get("image")), "") or offer.get("image") or ""
    if not src:
        return None
    if src.startswith("data:image/"):
        return src
    if src.startswith(("/", "~")):
        path = Path(src).expanduser().resolve()
        # Only pictures inside the report folder: a card must not pull arbitrary files into a page.
        if (not path.is_file() or path.suffix.lower() not in _IMAGE_EXT
                or REPORT_DIR.resolve() not in path.parents):
            return None
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()
    url = "https:" + src if src.startswith("//") else src
    try:
        if not _public_https(url):
            return None
        if any(h in url for h in ("alicdn.com", "aliexpress-media.com")):
            url = _cdn_picture_url(url)
        r = httpx.get(url, timeout=20.0, headers={"User-Agent": USER_AGENT}, follow_redirects=False)
        mime = r.headers.get("content-type", "").split(";")[0]
        if r.status_code != 200 or not r.content or not mime.startswith("image/"):
            return None
        return f"data:{mime};base64," + base64.b64encode(r.content).decode()
    except Exception:
        return None


@tool()
def make_report(name: str, title: str, sections: list[dict], subtitle: str = "",
                facts: list[dict] = None, notes: list[dict] = None) -> str:
    """
    Build THE report page for the user: always use this, never write report
    HTML by hand, so every report looks the same. Pictures are mandatory.

    Args:
        name: File name without extension (e.g. "tesla-camera-covers").
        title: Page title, a short name.
        subtitle: One line above the title (topic, date).
        facts: Summary figures at the top: [{"label": "Cart without import charge", "value": "32,02 €"}].
        notes: Callouts: [{"text": "...", "warn": true}] (warn = orange, for risks and open questions).
        sections: [{"heading": "...", "intro": "...", "cards": [card, ...]}] where card =
            {"title": "...", "item_id": "1005012122426554"  (picture is fetched from the product page)
             or "image": URL | /local/file.jpg | data: URI,
             "sku_id": optional, takes that variant's picture,
             "price": "3,15 €", "tags": ["Choice", "8.–13.11.", "✓ in cart"],
             "text": "one or two sentences", "url": link, "link_text": "...", "chosen": true}.
            A card without a resolvable picture makes the call fail and lists it.
    """
    for sec in sections:
        for c in sec.get("cards") or []:
            c.setdefault("title", c.get("item_id") or "?")
    cards = [c for sec in sections for c in sec.get("cards") or []]
    if not cards:
        return "No cards: give at least one section with cards."
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        for c, img in zip(cards, ex.map(_report_image, cards)):
            c["img"] = img
    for sec in sections:
        sec.setdefault("cards", [])
        sec.setdefault("heading", "")
    try:
        page = report.generic_page(title, subtitle, facts or [], notes or [], sections,
                                   time.strftime("%d.%m.%Y %H:%M"))
    except ValueError as e:
        return (f"Report not written: {e}. Give each of those cards an item_id or an image "
                "(URL, local file, data: URI), then call make_report again." + blocked_msg())
    return _write_report(f"{re.sub(r'[^A-Za-z0-9_-]+', '-', name).strip('-') or 'report'}.html", page)


@tool()
def decide_report(name: str, title: str, categories: list[dict], subtitle: str = "",
                  facts: list[dict] = None, notes: list[dict] = None,
                  choice_threshold: float = 10.0, choice_ship: float = 1.99) -> str:
    """
    Build a page the user DECIDES on (instead of make_report, which only shows): per category (one need
    each) the offers with Ja / Vielleicht / Nein, a quantity, a live estimate (goods + shipping + import charge) and a
    button that sends the choice to the chat. When the user's choice arrives (text plus a json block),
    pass it to apply_selection: it puts every "Ja" into the cart. Use it for any research with alternatives.
    Pictures are mandatory. Give the user the address; never publish the page.

    Args:
        name: File name without extension.
        title, subtitle, facts, notes: as in make_report.
        categories: [{"heading": "Frunk", "intro": "...", "cards": [card, ...]}], card =
            {"title": "...", "item_id": "...", "sku_id": "..." (both needed: the choice is put in the cart by them),
             "price": 20.39 (EUR per piece, number), "ship": 0 (EUR shipping for this line, 0 = free),
             "choice": true (Choice offer: shipping is shared, free from the threshold), "duty": 0 (estimated import charge per line),
             "qty": 1, "default": "yes"|"maybe"|"no" (pre-selected, default no), "tags": [...], "text": "...",
             "verdict": "your opinion in one sentence: why this one, what to watch" (ALWAYS fill it: he decides on it),
             "rating": "4,8★ 208" (when it matters),
             "fit": "✓ Highland genannt" (for things that must fit something: whether the seller names it), "url": link,
             "cart_id": only for lines already in the cart (see decide_cart)}.
            Keep "title" SHORT (what it is, a few words): the page shows one line per offer, the rest under "tags"/"text" on tap.
        choice_threshold, choice_ship: free Choice shipping from this order value, else this fee once.
    """
    for sec in categories:
        for c in sec.get("cards") or []:
            c.setdefault("title", c.get("item_id") or "?")
        sec.setdefault("cards", [])
        sec.setdefault("heading", "")
    cards = [c for sec in categories for c in sec["cards"]]
    if not cards:
        return "No cards: give at least one category with cards."
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        for c, img in zip(cards, ex.map(_report_image, cards)):
            c["img"] = img
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "decide"
    try:
        page = report.decide_page(safe, title, subtitle, facts or [], notes or [], categories,
                                  time.strftime("%d.%m.%Y %H:%M"), choice_threshold, choice_ship)
    except ValueError as e:
        return f"Page not written: {e}. Fix those cards, then call decide_report again." + blocked_msg()
    return _write_report(f"{safe}.html", page)


def _apply_cart_selection(cart: list[tuple[str, str, int, int]], dry_run: bool) -> str:
    """Lines already in the cart: delete = remove, yes = check for checkout, no/maybe = uncheck, quantity changes."""
    dele = [c for c, st, _, _ in cart if st == "delete"]
    keep = [(c, st, q, q0) for c, st, q, q0 in cart if st != "delete"]
    on = [c for c, st, _, _ in keep if st == "yes"]
    off = [c for c, st, _, _ in keep if st != "yes"]
    qty = [(c, q) for c, _, q, q0 in keep if q != q0]
    if dry_run:
        return (f"WOULD DELETE {len(dele)}, CHECK {len(on)}, UNCHECK {len(off)}, CHANGE QUANTITY of {len(qty)} cart line(s)")
    out = []
    if dele:
        out.append("Deleted: " + remove_from_cart(dele).split("\n")[0])
    for c, q in qty:
        out.append(f"Quantity {c} -> {q}: " + update_cart_quantity(c, q).split("\n")[0])
    def report(text: str) -> str:   # every line that says what happened or failed, not only the first
        return "\n  ".join(l for l in text.split("\n")
                            if not l.startswith(("Checkout link", "Kombi-Knaller cart now", "Cart page now")))
    if off:
        out.append("Unchecked: " + report(select_cart_items(off, only=False, selected=False)))
    if on:
        out.append("Checked: " + report(select_cart_items(on, only=False, selected=True)))
    return "\n".join(out) or "No change in the cart lines."


def _parse_selection(text: str) -> dict:
    """The json block of a decide_report message (fenced or bare) as a dict; ValueError when there is none."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m:
        raise ValueError("no json block in the selection")
    data = json.loads(m.group(1))
    if data.get("action") != "select" or not isinstance(data.get("items"), list):
        raise ValueError("not a selection (action select with items expected)")
    return data


@tool()
def apply_selection(selection: str, list_name: str = "", dry_run: bool = False) -> str:
    """
    Put the user's choice from a decide_report page into the cart: every item with state "yes" is added
    (and checked) with its quantity; "maybe" and "no" are left out and named. Pass the message the page sent
    (text plus json block) unchanged. Afterwards read the cart (view_cart) and tell the user the real prices
    against the page's estimate. Never orders anything.

    Args:
        selection: The whole message from the page.
        list_name: Shopping list the lines belong to (see set_cart_list); empty = none.
        dry_run: Only list what would be added.
    """
    try:
        data = _parse_selection(selection)
    except (ValueError, json.JSONDecodeError) as e:
        return f"Cannot read the selection: {e}. Pass the page's whole message, including its json block."
    yes, rest, cart = [], [], []
    for it in data["items"]:
        try:
            item_id, sku_id = str(int(str(it["item_id"]))), str(int(str(it["sku_id"])))
            qty = max(1, min(99, int(it.get("qty") or 1)))
            cid = str(int(str(it["cart_id"]))) if it.get("cart_id") else ""
            raw = it.get("replaces") or []
            repl = [str(int(str(x).strip())) for x in (raw.split(",") if isinstance(raw, str) else raw) if str(x).strip()]
            qty0 = int(it.get("qty0") or qty)
        except (KeyError, ValueError, TypeError):
            return f"Bad item in the selection: {it!r}. Nothing was changed."
        if it.get("state") not in ("yes", "maybe", "no", "delete"):
            return f"Bad state in the selection: {it!r}. Nothing was changed."
        if cid:
            cart.append((cid, it["state"], qty, qty0))
        else:
            (yes if it.get("state") == "yes" else rest).append(
                (item_id, sku_id, qty, str(it.get("category") or ""), repl, bool(it.get("combo"))))
    if not yes and not cart:
        return "Nothing chosen with Ja: nothing added."
    out = [_apply_cart_selection(cart, dry_run)] if cart else []
    for item_id, sku_id, qty, cat, repl, combo in yes:
        if dry_run:
            out.append(f"WOULD ADD {qty} x {item_id} / {sku_id} ({cat})" + (f", then REMOVE cart line(s) {', '.join(repl)}" if repl else ""))
            continue
        res = add_to_cart(item_id, sku_id, qty, list_name=list_name, need=cat, combo=combo)
        out.append(f"{item_id} / {sku_id} x{qty}: " + res)
        if repl:   # the offer replaces a line (a cheaper way to buy the same): drop the old line only when the new one is in
            out.append(f"Replaced line(s) {', '.join(repl)}: " + (remove_from_cart(repl) if res.startswith("Added") else "NOT removed, the add failed"))
    left = [f"{i}/{s}" for i, s, _, _, _, _ in rest]
    return "\n".join(out + ([f"Not added (Vielleicht or Nein): {', '.join(left)}"] if left else []))


@tool(slow=True)
def decide_cart(name: str = "warenkorb", title: str = "Warenkorb", list_name: str = "",
                opinions: dict = None, check_deals: bool = True, approved_alternatives: list = None,
                job_id: str = "") -> str:
    """
    The REAL cart as a decision page, compact enough for 50 lines: per shopping list (and the Kombi-Knaller
    basket) one row per line with picture, short title, YOUR opinion, price and Ja / Nein / Löschen
    (Ja = checked for checkout, Nein = unchecked, Löschen = removed from the cart). Use it instead of
    describing a big cart in prose: the user goes through it once, presses the send button, and the
    message he sends goes unchanged to apply_selection, which checks, unchecks, deletes and changes
    quantities. Then read the cart again and report the real total. Give the user the page's server address
    as plain text (see SKILL.md).

    ALWAYS fill `opinions`: the user decides on your judgement. Without it the rows are bare.

    Slow with check_deals: every line is searched and its hits priced. It runs as a background job: the call waits up
    to a minute; when not finished it answers with progress and a job_id: call decide_cart(job_id=...) again later.

    Args:
        name: File name without extension.
        title: Page title.
        list_name: Only this shopping list (empty = everything).
        opinions: {cart_id: {"verdict": "one sentence: why this one, what to watch", "rating": "4,8★ 208" (when it matters),
            "fit": "✓ Highland genannt" or "Modell nicht genannt" (only for parts that must fit something)}}.
            Lines you know nothing about may be left out.
        check_deals: before the page is built, EVERY line is compared with other sellers, Choice offers and
            Kombi-Knaller offers (same pieces, shipping, import charge and discounts included). Only an offer that is strictly
            cheaper and approved goes on the page: a row right under the line, pre-selected, replacing the line.
            Costs searches (cached 6 h); False = skip.
        approved_alternatives: cart_ids whose alternative YOU have verified. A found alternative is NOT put on the page until
            you approve it: the answer lists every candidate with a comparison picture (original left, offer right) in the
            report folder. Read each picture and the numbers, decide whether it is the SAME product (same shape, same
            variant, same pack size), then call decide_cart again with the cart_ids that are. Search hits are often
            different articles (an accessory for 0,44 € against a 20 € part): never approve unseen.
        job_id: pick up a running or finished job (see above).
    """
    args = (name, title, list_name, opinions, check_deals, approved_alternatives)
    if not check_deals and not job_id:
        return _decide_run(*args, lambda i, n, what: None)
    return _job_result(job_id, "decide", lambda progress: _decide_run(*args, progress))


def _decide_run(name, title, list_name, opinions, check_deals, approved_alternatives, progress) -> str:
    cookies = load_cookies()
    if not cookies:
        return AUTH_EXPIRED_MSG
    try:
        c = _read_cart(cookies)
    except Exception as e:
        return f"Cart not readable: {e}"
    tags = _line_store()
    groups: dict[str, list[dict]] = {}
    for ln in c["lines"]:
        if not ln.get("valid", True):
            continue
        tag = tags.get(ln["cart_id"]) or {}
        if list_name and tag.get("list") != list_name:
            continue
        groups.setdefault(tag.get("list") or "Ohne Liste", []).append((ln, tag))
    combo_lines = _combo_lines(cookies, c) or []
    if combo_lines and not list_name:
        groups["Kombi-Knaller-Korb"] = [(ln, tags.get(ln["cart_id"]) or {}) for ln in combo_lines if ln["valid"]]
    if not groups:
        return "The cart has no lines to show."
    cats = []
    alt_cache = _alt_cache() if check_deals else {}
    approved = {str(x) for x in (approved_alternatives or [])}
    pending: list[tuple[dict, dict, dict, str]] = []   # (original card, alt, line, why not shown)
    merge_notes: list[str] = []
    found: list[str] = []                              # every better deal, shown or pending
    # Pass 1: merges per group and the deal of every other line; pass 2 decides (a Kombi price needs the piece total).
    merges_of: dict[str, list[dict]] = {}
    deal_of: dict[str, dict] = {}
    for heading, lines in groups.items():
        merges_of[heading] = []
        if check_deals and heading != "Kombi-Knaller-Korb":
            try:
                merges_of[heading] = _merge_split_lines([(ln, tag) for ln, tag in lines], alt_cache)
            except Exception as e:
                logger.debug("merging split lines failed: %s", e)
    todo = [(ln, tag) for heading, lines in groups.items() if check_deals and heading != "Kombi-Knaller-Korb"
            for ln, tag in lines if not any(ln in mg["lines"] for mg in merges_of[heading])]
    for i, (ln, tag) in enumerate(todo):
        progress(i, len(todo), (tag.get("need") or ln["title"])[:40])
        try:
            d = _line_deal(ln, tag, alt_cache)
        except PermissionError:
            return AUTH_EXPIRED_MSG
        except Exception as e:
            logger.debug("deal for %s failed: %s", ln["cart_id"], e)
            d = None
        if d:
            deal_of[ln["cart_id"]] = {**d, "floor": 0.4 * cartlib.goods(ln)}   # under 40 % of the goods: bait, never a winner
    pcs_of = lambda ln: max(1, ln["qty"] * deals.pack_size(ln.get("variant")))
    combo_pcs = sum(pcs_of(ln) for ln, _ in groups.get("Kombi-Knaller-Korb", []))

    def pick(kombi_counts: bool) -> dict[str, dict]:
        out = {}
        for cid, d in deal_of.items():
            w = min([x for x in (d["best"], d["kombi"] if kombi_counts else None) if x and x["cost"] >= d["floor"]],
                    key=lambda x: x["cost"], default=None)
            if w and d["now"] - w["cost"] > 0.004:
                out[cid] = w
        return out
    win_of = pick(True)
    if combo_pcs + sum(w["pieces"] for w in win_of.values() if w["combo"]) < COMBO_MIN_PIECES:
        win_of = pick(False)          # below 3 combo pieces the import charge stays: a Kombi offer only counts at its normal net
    for heading, lines in groups.items():
        cards = []
        merges = merges_of[heading]
        merged_of = {ln["cart_id"]: mg for mg in merges for ln in mg["lines"]}
        for ln, tag in lines:
            need = tag.get("need") or ln["title"][:40]
            op = (opinions or {}).get(ln["cart_id"]) or (opinions or {}).get(ln["item_id"]) or {}
            cards.append({"title": f"{need} · {ln['variant']}" if ln.get("variant") else need,
                          "verdict": op.get("verdict") or "", "rating": op.get("rating") or "", "fit": op.get("fit") or "",
                          "item_id": ln["item_id"], "sku_id": ln["sku_id"], "cart_id": ln["cart_id"],
                          "price": ln["price"] or 0, "ship": ln.get("ship") or 0, "choice": ln.get("group") == "group_choice",
                          "duty": DUTY_PER_LINE if (heading != "Kombi-Knaller-Korb" and ln.get("group") != "group_choice") else 0,
                          "qty": ln["qty"], "default": "yes" if ln["checked"] else "no",
                          "image": ln.get("image") or "", "url": f"{BASE_URL}/item/{ln['item_id']}.html",
                          "tags": [x for x in (cartlib.GROUP_NAMES.get(ln.get("group"), ln.get("group")),
                                               f"{ln['delivery'][0]}…{ln['delivery'][1]}" if ln.get("delivery") else "") if x],
                          "text": ln["title"]})
            mg = merged_of.get(ln["cart_id"])
            if mg:
                win, c0 = mg["win"], cards[-1]
                c0["state0"] = c0["default"]
                if mg["keep"] is ln:
                    c0.update(qty=win["qty"], qty0=ln["qty"], default="yes", badge="Zusammengelegt",
                              verdict=f"{win['qty']}× in einer Zeile statt {len(mg['lines'])} Zeilen: spart {report.eur(mg['saving'])}")
                else:
                    c0.update(default="delete" if mg["keep"] else "no",
                              verdict=f"Zusammengelegt: ein Bedarf ({mg['total']} Stück) ist in einer Zeile günstiger, spart {report.eur(mg['saving'])}")
                continue
            w = win_of.get(ln["cart_id"])
            if w:
                q, now = w["qty"], deal_of[ln["cart_id"]]["now"]
                diff = now - w["cost"]
                line = (f"{need} ({ln['cart_id']}): now {report.eur(now)} -> {w['buy']} for {report.eur(w['cost'])}, "
                        f"saves {report.eur(diff)}" + (" (Kombi-Knaller)" if w["combo"] else ""))
                w = {**w, "now": now}
                nomatch = "; no match words: verify it is the same article" if not tag.get("match") else ""
                if w["cost"] < 0.4 * cartlib.goods(ln):
                    pending.append((cards[-1], w, ln, "rejected: price under 40 % of the goods, a different article"))
                    found.append(f"- {line} - REJECTED, a different article")
                    continue
                if ln["cart_id"] not in approved:
                    pending.append((cards[-1], w, ln, "not verified yet" + nomatch))
                    found.append(f"- {line} - not verified yet" + (" (no match words on this line)" if nomatch else ""))
                    continue
                found.append(f"- {line} - on the page")
                when = f"Lieferung in etwa {w['days']} Tagen" if w.get("days") else ""
                kombi = "im Kombi-Knaller-Korb, ab 3 Teilen ohne Abgabe. " if w["combo"] else ""
                cards[-1]["default"] = "no"
                cards.append({"title": f"{need} · {w['buy']}", "badge": "Kombi-Knaller" if w["combo"] else "Günstiger", "item_id": w["item_id"],
                              "sku_id": w["sku_id"], "replaces": ln["cart_id"], "price": round(w["cost"] / q, 2), "ship": 0,
                              "choice": w["choice"], "duty": 0, "qty": q, "default": "yes", "combo": w["combo"],
                              "verdict": (f"{w['buy']} für {report.eur(w['cost'])} statt {report.eur(now)} (Ware, Versand, Abgabe): "
                                          f"spart {report.eur(diff)}. {kombi}{when}").strip(),
                              "fit": "Variantenname mehrfach: Bild prüfen" if w.get("ambiguous") else "",
                              "image": w.get("img") or "", "url": w["url"],
                              "tags": ["Günstiger"] + (["Kombi-Knaller"] if w["combo"] else (["Choice"] if w["choice"] else [])),
                              "text": w["title"]})
        for mg in merges:
            win, need = mg["win"], mg["need"]
            old = " + ".join(f"{ln['qty']}× {ln.get('variant') or ln['title'][:30]}" for ln in mg["lines"])
            if mg["keep"] is None:
                cards.append({"title": f"{need} · {win['buy']}", "badge": "Zusammengelegt", "item_id": win["item_id"], "sku_id": win["sku_id"],
                              "replaces": [ln["cart_id"] for ln in mg["lines"]], "price": round(win["cost"] / win["qty"], 2), "ship": 0,
                              "choice": False, "duty": 0, "qty": win["qty"], "default": "yes",
                              "verdict": f"{win['buy']} in einer Zeile für {report.eur(win['cost'])} statt {old} für {report.eur(mg['split'])}: spart {report.eur(mg['saving'])}",
                              "image": win.get("img") or "", "url": win["url"], "tags": ["Zusammengelegt"], "text": win["title"]})
            merge_notes.append(f"- {need}: {old} -> {win['buy']} in ONE line ({mg['total']} pieces needed), "
                               f"{report.eur(mg['split'])} -> {report.eur(win['cost'])}, saves {report.eur(mg['saving'])}"
                               + (f"; kept cart line {mg['keep']['cart_id']} (quantity change)" if mg["keep"] else "; new offer replaces the old lines"))
        cats.append({"heading": heading, "cards": cards})
    if alt_cache:
        private.write_json(ALT_CACHE_FILE, alt_cache)
    cards = [x for cat in cats for x in cat["cards"]]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        for x, img in zip(cards, ex.map(_report_image, cards)):
            x["img"] = img
    for x in cards:
        if not x["img"]:
            x["img"] = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=="   # a blank pixel
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "warenkorb"
    page = report.decide_page(safe, title, time.strftime("%d.%m.%Y %H:%M"), [], [], cats, time.strftime("%d.%m.%Y %H:%M"))
    msg = _write_report(f"{safe}.html", page)
    todo = []
    for orig, alt, ln, why in pending:
        if why.startswith("rejected"):
            todo.append(f"- {ln['cart_id']} {orig['title'][:40]}: candidate '{alt['title'][:50]}' for {report.eur(alt['cost'])} "
                        f"against {report.eur(cartlib.goods(ln))} — {why}.")
            continue
        pic = _compare_image(ln["cart_id"], orig.get("img"), _report_image({"image": alt.get("img") or ""}))
        todo.append(f"- {ln['cart_id']} {orig['title'][:40]}: candidate '{alt['title'][:50]}' ({alt['buy']}"
                    + (", Kombi-Knaller" if alt["combo"] else "") + f") for {report.eur(alt['cost'])} against {report.eur(alt['now'])} now"
                    + (" - NO MATCH WORDS on this line: verify it is the same article" if "no match words" in why else "") + ". "
                    + (f"Comparison picture, READ IT: {pic}" if pic else "No comparison picture: look at both offers yourself."))
    if found:
        msg += ("\n\nBETTER DEALS FOUND (every line was compared with other sellers, Choice and Kombi-Knaller; "
                "only strictly cheaper offers are listed):\n" + "\n".join(found))
    if merge_notes:
        msg += "\n\nMERGED LINES (one need split over several cart lines is cheaper as one line; already on the page, tell the user):\n" + "\n".join(merge_notes)
    if todo:
        msg += ("\n\nCHEAPER-WAY CANDIDATES, NOT ON THE PAGE YET. Read each comparison picture (original left, candidate right). "
                "Only when it is the same product in the same variant and pack, call decide_cart again with "
                "approved_alternatives=[cart_ids] (the same opinions); say to the user which you approved and which not and why:\n"
                + "\n".join(todo))
    return msg


# ─── Orders (read only) ─────────────────────────────────────────────────────

ORDERS_REFERER = "https://www.aliexpress.com/p/order/index.html"


def _orders_call(payload: dict, post: bool = False, api: str = "") -> dict:
    resp = mtop_call(api or orderslib.LIST_API, "1.0", payload, referer=ORDERS_REFERER,
                     method="POST" if post else "GET")
    ret = " ".join(resp.get("ret") or [])
    if "SESSION_EXPIRED" in ret:
        raise PermissionError("auth expired")
    if "SUCCESS" not in ret:
        raise RuntimeError(f"Order API returned: {ret or resp}")
    return resp


def _fetch_orders(tab: str, since: str, max_pages: int) -> tuple[list[dict], bool]:
    """Orders of one tab, newest first, page by page. (orders, complete?)"""
    resp = _orders_call(orderslib.init_payload(tab))
    found: dict[str, dict] = {}
    complete = True
    for n in range(1, max_pages + 1):
        page, state = orderslib.parse_list(resp)
        for o in page:
            found.setdefault(o["order_id"], o)
        if not state["has_more"] or not page:
            break
        if since and all(o["date"] and o["date"] < since for o in page):
            break  # the list is newest first: everything after this page is older
        if n == max_pages:
            complete = False
            break
        resp = _orders_call(orderslib.next_payload(state, tab), post=True)
    return list(found.values()), complete


def _order_csv(rows: list[dict], path: Path) -> None:
    import csv
    cols = ["order_id", "date", "status", "status_text", "store", "order_total", "currency", "item_id", "sku_id",
            "title", "variant", "quantity", "unit_price", "eta", "tracking", "image", "url", "order_url"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, cols)
        w.writeheader()
        for o in rows:
            for it in o["items"] or [{}]:
                w.writerow({"order_id": o["order_id"], "date": o["date"], "status": o["status"],
                            "status_text": o["status_text"], "store": o["store"], "order_total": o["total"],
                            "currency": o["currency"], "eta": o.get("eta", ""),
                            "tracking": " ".join(o.get("tracking") or []), "order_url": o["url"],
                            **{k: it.get(k, "") for k in ("item_id", "sku_id", "title", "variant", "quantity",
                                                           "unit_price", "image", "url")}})


@tool()
def list_orders(status: str = "", since: str = "", out: str = "", details: str = "open",
                max_pages: int = 30, report_limit: int = 40, include_cancelled: bool = False) -> str:
    """
    Read "My orders" (read only: nothing is paid, confirmed or changed).

    Writes machine-readable files and a report page with pictures:
    orders.json (and orders.csv, one row per ordered item) in the report folder
    or at `out` (a .json path; the .csv goes next to it). Per order: number, date,
    total, normalised status plus the original AliExpress text, store, link and
    the items (item_id, title, variant/sku, quantity, unit price, picture, link);
    after `details` also expected delivery date, tracking numbers, shipping and
    import charges. The delivery address is never included.

    Args:
        status: Only this state: wartet_auf_zahlung, in_bearbeitung, versandt, zugestellt,
            abgeschlossen, storniert, rueckerstattung. Empty = all except cancelled orders
            (they are irrelevant and hidden unless asked for).
        since: Only orders placed on/after this date (YYYY-MM-DD).
        out: Path of the JSON file (default <report folder>/orders.json).
        details: "open" = delivery date and tracking for orders that are not completed or
            cancelled (default), "all" = for every order (one call each, slow), "none".
        max_pages: Pages of 10 orders to read at most.
        report_limit: Items shown on the report page (the files have everything).
        include_cancelled: Also list cancelled orders when `status` is empty (status="storniert" shows them too).
    """
    status = status.strip().lower()
    if status and status not in orderslib.STATES:
        return f"Unknown status '{status}'. One of: {', '.join(orderslib.STATES)}."
    if since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        return "since must look like 2026-10-01."
    if not load_cookies():
        return AUTH_EXPIRED_MSG
    try:
        tab = orderslib.TAB_FOR.get(status, "all")
        rows, complete = _fetch_orders(tab, since, max(1, max_pages))
        hide = not status and not include_cancelled
        rows = [o for o in rows if (o["status"] == status if status else not (hide and o["status"] == "storniert"))
                and (not since or (o["date"] and o["date"] >= since))]
        if details in ("open", "all"):
            for o in rows:
                if details == "open" and o["status"] in ("abgeschlossen", "storniert", "rueckerstattung"):
                    continue
                try:
                    o.update(orderslib.parse_detail(_orders_call(orderslib.detail_payload(o["order_id"]),
                                                                  api=orderslib.DETAIL_API)))
                except PermissionError:
                    raise
                except Exception as e:  # one broken order must not throw away the list
                    o["detail_error"] = str(e)
    except PermissionError:
        return AUTH_EXPIRED_MSG
    except Exception as e:
        return f"Reading the orders failed: {e}" + blocked_msg()

    path = Path(out).expanduser() if out else REPORT_DIR / "orders.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"read_at": time.strftime("%Y-%m-%d %H:%M"), "complete": complete,
                                "filter": {"status": status, "since": since}, "orders": rows},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    csv_path = path.with_suffix(".csv")
    _order_csv(rows, csv_path)

    counts: dict[str, int] = {}
    for o in rows:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    lines = [f"{len(rows)} order(s)" + ("" if complete else f" (stopped after {max_pages} pages, more exist)")
             + (": " + ", ".join(f"{n}× {k}" for k, n in sorted(counts.items())) if counts else ""),
             f"JSON: {path}", f"CSV: {csv_path}"]
    for o in rows[:25]:
        first = (o["items"] or [{}])[0]
        lines.append(f"- {o['date']} #{o['order_id']} [{o['status']}] {o['total']} {o['currency']} · "
                     f"{(first.get('title') or '')[:60]}" + (f" +{len(o['items']) - 1}" if len(o["items"]) > 1 else "")
                     + (f" · ETA {o['eta']}" if o.get("eta") else "") + (f" · {', '.join(o['tracking'])}" if o.get("tracking") else ""))
    if len(rows) > 25:
        lines.append(f"… {len(rows) - 25} more in the files")
    if rows and report_limit > 0 and any(o["items"] for o in rows):
        lines.append(_orders_report(rows, report_limit))
    return "\n".join(lines)


def _orders_report(rows: list[dict], limit: int) -> str:
    sections: dict[str, list[dict]] = {}
    shown = 0
    for o in rows:
        for it in o["items"]:
            if shown >= limit:
                break
            shown += 1
            tags = [o["status_text"] or o["status"], o["date_text"] or o["date"]]
            if o.get("eta"):
                tags.append("ETA " + o["eta"])
            if o.get("tracking"):
                tags.append("Tracking " + ", ".join(o["tracking"]))
            unit = it.get("unit_price")
            sections.setdefault(o["status"], []).append({
                "title": it["title"][:110], "image": it["image"] or None, "item_id": it["item_id"],
                "price": f"{it['quantity']} × {unit:.2f} {o['currency']}" if unit is not None else "",
                "tags": tags, "text": f"{it['variant']} · Order #{o['order_id']} · {o['store']}".strip(" ·"),
                "url": o["url"], "link_text": "Order"})
    secs = [{"heading": k.replace("_", " "), "cards": v} for k, v in sections.items()]
    sums: dict[str, float] = {}
    for o in rows:
        sums[o["currency"] or "?"] = sums.get(o["currency"] or "?", 0) + (o["total"] or 0)
    facts = [{"label": "Orders", "value": str(len(rows))},
             {"label": "Sum", "value": " + ".join(f"{v:.2f} {c}" for c, v in sums.items())}]
    notes = [{"text": f"Page shows the first {limit} items; orders.json has all."}] if shown >= limit else []
    return make_report("orders", "My orders", secs, subtitle=time.strftime("%d.%m.%Y"), facts=facts, notes=notes)


# ─── Main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
