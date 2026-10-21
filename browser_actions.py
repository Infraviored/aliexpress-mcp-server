"""
Actions AliExpress only accepts from its own page: the page signs them with
browser fingerprints (`ua`, `umidToken`) that a plain API call cannot make.
Without them `ump.coupon.common.assignNew` answers SUCCESS and does nothing.

Runs real Chrome on a virtual display (like slider.py) with the credential
cookies and clicks what a person would click.
"""

import json
import logging
import os
import time
from pathlib import Path

import slider

logger = logging.getLogger("aliexpress-mcp")

COUPON_ENTRY = "[class*=coupon-block--wrap]"
COLLECT_BUTTON = '[ae_button_type="Promotion_shopCoupon_collect"][ae_object_value*="activityId={id};"]'


def collect_coupons(item_id: str, campaign_ids: list[str], credentials_path: Path, timeout_s: float = 60) -> dict[str, str]:
    """
    Collect the given store coupons (campaign ids from the product's discount
    panel) on the product page. Returns {campaign_id: "collected" | reason}.
    Only plain "Collect" coupons: "Follow & collect" would follow the store.
    """
    from playwright.sync_api import sync_playwright

    cookies = json.loads(credentials_path.read_text()).get("cookies", {})
    results: dict[str, str] = {str(c): "not found on the page" for c in campaign_ids}
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
                ctx = browser.new_context(viewport={"width": 1366, "height": 900})
                ctx.add_cookies([
                    {"name": k, "value": v, "domain": ".aliexpress.com", "path": "/"}
                    for k, v in cookies.items()
                ])
                page = ctx.new_page()
                page.goto(f"https://www.aliexpress.com/item/{item_id}.html",
                          wait_until="domcontentloaded", timeout=int(timeout_s * 1000))
                page.locator(COUPON_ENTRY).first.wait_for(state="visible", timeout=int(timeout_s * 1000))
                time.sleep(2)
                page.locator(COUPON_ENTRY).first.click()
                time.sleep(3)
                for cid in results:
                    btn = page.locator(COLLECT_BUTTON.format(id=cid))
                    if not btn.count():
                        continue
                    try:
                        with page.expect_response(lambda r: "coupon.common.assignnew" in r.url.lower(),
                                                  timeout=15000) as info:
                            btn.first.click()
                        data = (info.value.json().get("data") or {})
                        if data.get("assignSuccessList"):
                            results[cid] = "collected"
                        else:
                            fail = (data.get("assignFailList") or [{}])[0]
                            results[cid] = fail.get("failMsg") or fail.get("errorMsg") or "refused"
                    except Exception as e:
                        results[cid] = f"click failed: {e}"
                    time.sleep(1.5)
            finally:
                browser.close()
    finally:
        if xvfb:
            xvfb.terminate()
    return results
