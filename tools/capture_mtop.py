"""
Record the MTOP calls a real AliExpress page makes, to learn an API before
calling it directly.

Opens `url` in real Chrome (on Xvfb, like slider.py) with the credential
cookies, runs a list of steps, and writes every acs.aliexpress.com request
(api, method, data) and its response to a JSON file. Cookies and signatures
are not logged — but response bodies are, whole: recording checkout, address
or account pages puts the address and buyer id into the file. Keep such
recordings out of the repository.

    python tools/capture_mtop.py URL OUT.json [STEP ...]

Steps:
    wait:SECONDS            sleep
    scroll                  scroll to the bottom (pagination)
    click:SELECTOR          click the first match (Playwright selector)
    clicknth:N:SELECTOR     click the N-th match (0-based)
    shot:PATH               screenshot
    block:GLOB              abort requests matching GLOB from now on and log
                            their URL (e.g. keep a click from leaving the page)

Example (toggle the first cart line's checkbox twice):
    python tools/capture_mtop.py https://www.aliexpress.com/p/shoppingcart/index.html out.json \
        wait:6 "clicknth:1:.cart-product .comet-v2-checkbox" wait:3 \
        "clicknth:1:.cart-product .comet-v2-checkbox" wait:3
"""

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import slider  # noqa: E402

DROP = {"sign", "t", "appKey", "jsv", "_m_h5_tk", "_m_h5_tk_enc"}


def _record(entries: list, request, response=None) -> None:
    if "acs.aliexpress.com" not in request.url and "/h5/mtop." not in request.url:
        return
    q = parse_qs(urlparse(request.url).query)
    data = (q.get("data") or [None])[0]
    if request.method == "POST":
        body = request.post_data or ""
        form = parse_qs(body)
        data = (form.get("data") or [body])[0]
    try:
        data = json.loads(data) if data else None
    except (TypeError, ValueError):
        pass
    entry = {
        "api": (q.get("api") or [urlparse(request.url).path])[0],
        "v": (q.get("v") or [None])[0],
        "method": request.method,
        "data": data,
    }
    if response is not None:
        try:
            entry["response"] = response.json()
        except Exception:
            entry["response"] = None
    entries.append(entry)


def capture(url: str, out: Path, steps: list[str]) -> int:
    from playwright.sync_api import sync_playwright

    creds = Path(os.environ.get("ALIEXPRESS_CREDENTIALS", "~/.mcp-credentials/aliexpress.json")).expanduser()
    cookies = json.loads(creds.read_text()).get("cookies", {})
    entries: list = []
    xvfb, display = slider._start_xvfb()
    env = dict(os.environ)
    if display:
        env["DISPLAY"] = display
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel="chrome", headless=False, env=env,
                args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
            )
            try:
                ctx = browser.new_context(viewport={"width": 1366, "height": 900}, locale="en-US")
                ctx.add_cookies([
                    {"name": k, "value": v, "domain": ".aliexpress.com", "path": "/"}
                    for k, v in cookies.items()
                ])
                page = ctx.new_page()
                page.on("response", lambda r: _record(entries, r.request, r))
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                for step in steps:
                    kind, _, arg = step.partition(":")
                    print(f"step {step}", file=sys.stderr)
                    if kind == "wait":
                        time.sleep(float(arg or 2))
                    elif kind == "scroll":
                        page.mouse.wheel(0, 20000)
                        time.sleep(2)
                    elif kind == "click":
                        page.locator(arg).first.click()
                    elif kind == "clicknth":
                        n, _, sel = arg.partition(":")
                        page.locator(sel).nth(int(n)).click()
                    elif kind == "block":
                        def _block(route, entries=entries):
                            entries.append({"api": "blocked", "method": route.request.method,
                                            "url": route.request.url, "data": None})
                            route.abort()
                        page.route(arg, _block)
                    elif kind == "shot":
                        page.screenshot(path=arg)
                    else:
                        raise SystemExit(f"unknown step {step}")
            finally:
                browser.close()
    finally:
        if xvfb:
            xvfb.terminate()
    for e in entries:
        if isinstance(e.get("data"), dict):
            for k in DROP:
                e["data"].pop(k, None)
    out.write_text(json.dumps(entries, ensure_ascii=False, indent=1))
    for e in entries:
        print(f"{e['method']:4} {e['api']} {e.get('url', '')}", file=sys.stderr)
    return len(entries)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    n = capture(sys.argv[1], Path(sys.argv[2]), sys.argv[3:])
    print(f"{n} MTOP call(s) -> {sys.argv[2]}")
