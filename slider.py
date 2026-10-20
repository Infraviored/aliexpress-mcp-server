"""
Solve AliExpress' "drag the slider to verify" bot check (FAIL_SYS_USER_VALIDATE).

MTOP answers a throttled call with `data.url` pointing at a `_____tmd_____/punish`
page that holds an Alibaba nocaptcha slider. We open that page in real Chrome on
a virtual display (headless Chrome is recognised and refused), drag the knob to
the end like a hand would, and merge the cookies the page sets (`x5sec` is the
pass) back into the credential file.

Needs: playwright (pip), google-chrome, Xvfb.
"""

import json
import logging
import os
import random
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("aliexpress-mcp")

KNOB = "#nc_1_n1z"
TRACK = "#nc_1__scale_text"


def _start_xvfb() -> tuple[subprocess.Popen | None, str | None]:
    """Start Xvfb on a free display unless one is already set."""
    if os.environ.get("DISPLAY") or not shutil.which("Xvfb"):
        return None, os.environ.get("DISPLAY")
    for n in range(90, 120):
        if not Path(f"/tmp/.X11-unix/X{n}").exists() and not Path(f"/tmp/.X{n}-lock").exists():
            proc = subprocess.Popen(
                ["Xvfb", f":{n}", "-screen", "0", "1366x900x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(0.8)
            return proc, f":{n}"
    raise RuntimeError("No free X display for Xvfb")


def _drag(page, distance: float, start_x: float, y: float) -> None:
    """Accelerate, then ease out, with small vertical jitter and pauses."""
    page.mouse.move(start_x - random.uniform(30, 60), y + random.uniform(-15, 15))
    time.sleep(random.uniform(0.2, 0.4))
    page.mouse.move(start_x, y, steps=random.randint(5, 9))
    time.sleep(random.uniform(0.1, 0.25))
    page.mouse.down()
    time.sleep(random.uniform(0.08, 0.2))
    steps = random.randint(28, 40)
    target = distance + random.uniform(4, 10)  # a little past the end is fine
    for i in range(1, steps + 1):
        t = i / steps
        eased = 1 - (1 - t) ** 3
        page.mouse.move(start_x + target * eased, y + random.uniform(-1.5, 1.5))
        time.sleep(random.uniform(0.008, 0.03))
    time.sleep(random.uniform(0.15, 0.35))
    page.mouse.up()


def solve(punish_url: str, credentials_path: Path, timeout_s: float = 60) -> bool:
    """Pass the slider at `punish_url`; merge new cookies into `credentials_path`."""
    from playwright.sync_api import sync_playwright

    data = json.loads(credentials_path.read_text())
    cookies: dict[str, str] = data.get("cookies", {})

    xvfb, display = _start_xvfb()
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
                ctx = browser.new_context(viewport={"width": 1280, "height": 860}, locale="en-US")
                ctx.add_cookies([
                    {"name": k, "value": v, "domain": ".aliexpress.com", "path": "/"}
                    for k, v in cookies.items()
                ])
                page = ctx.new_page()
                page.goto(punish_url, wait_until="networkidle", timeout=int(timeout_s * 1000))

                passed = False
                for attempt in range(3):
                    knob = page.locator(KNOB)
                    knob.wait_for(state="visible", timeout=15000)
                    kb = knob.bounding_box()
                    tb = page.locator(TRACK).bounding_box()
                    if not kb or not tb:
                        break
                    distance = tb["x"] + tb["width"] - kb["x"] - kb["width"] / 2
                    _drag(page, distance, kb["x"] + kb["width"] / 2, kb["y"] + kb["height"] / 2)

                    deadline = time.time() + 8
                    while time.time() < deadline:
                        if any(c["name"] == "x5sec" for c in ctx.cookies()):
                            passed = True
                            break
                        # Failed attempt: nocaptcha shows a "click to refresh" link.
                        refresh = page.locator("#nc_1_refresh1, .nc_iconfont.icon_warn, #nc_1__scale_text a")
                        if refresh.count():
                            refresh.first.click()
                            time.sleep(1.0)
                            break
                        time.sleep(0.3)
                    if passed:
                        break
                    logger.info("slider attempt %d did not pass", attempt + 1)

                if not passed:
                    return False

                time.sleep(1.0)  # let the page finish setting cookies
                for c in ctx.cookies():
                    if "aliexpress" in c["domain"]:
                        cookies[c["name"]] = c["value"]
            finally:
                browser.close()
    finally:
        if xvfb:
            xvfb.terminate()

    data["cookies"] = cookies
    tmp = credentials_path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        tmp.replace(credentials_path)
    finally:
        tmp.unlink(missing_ok=True)
    return True
