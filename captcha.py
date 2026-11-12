"""
Pass an AliExpress image bot check (Google reCAPTCHA "select all images with ...") with Florian
choosing the tiles: the browser does the clicking, he does the deciding.

  captcha.py start [punish-url]   open the check in Chrome on a virtual display and keep it open
                                  (run in the background); prints the picture path when ready
  captcha.py answer 2 3 8         click these tiles (numbers as on the picture); "v" = Verify only
  captcha.py status               what the open check is doing

The picture (`shot.png`) shows only the blue task and the tiles, each numbered. The agent shows it
in the chat (Read on the PNG), Florian names the tiles, the agent passes them on with `answer`.
On success the cookies are merged into the credential file and the pause ends.
The punish URL defaults to the last one AliExpress sent (aliexpress-botcheck.json).
"""

import json
import os
import random
import sys
import time
from pathlib import Path

import private

CRED = Path(os.environ.get("ALIEXPRESS_CREDENTIALS", Path.home() / ".mcp-credentials" / "aliexpress.json"))
DIR = CRED.parent / "captcha"
SHOT, ANSWER, STATE = DIR / "shot.png", DIR / "answer.txt", DIR / "state.json"
BOT_FILE = CRED.parent / "aliexpress-botcheck.json"
WAIT_ANSWER_S = 15 * 60


def _state(status: str, **kw) -> None:
    private.write_json(STATE, {"status": status, "at": time.time(), "pid": os.getpid(), **kw})


def _read_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def _numbered(png: bytes, tiles: list[dict], clip: dict) -> bytes:
    """The cropped picture with a small number in the corner of each tile."""
    import io
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(io.BytesIO(png)).convert("RGB")
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    for i, t in enumerate(tiles, 1):
        x, y = t["x"] - clip["x"] + 4, t["y"] - clip["y"] + 4
        d.rectangle([x, y, x + (30 if i > 9 else 20), y + 26], fill=(0, 0, 0))
        d.text((x + 3, y + 1), str(i), fill=(255, 255, 0), font=font)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def _challenge(page):
    """The image-challenge frame when it is showing, else None."""
    el = page.locator("iframe[src*='recaptcha'][src*='bframe']")
    if not el.count() or (el.first.bounding_box() or {}).get("height", 0) < 100:
        return None
    return next((f for f in page.frames if "recaptcha" in f.url and "bframe" in f.url), None)


def _snapshot(page, frame) -> list[dict]:
    """Save the numbered picture of task + tiles; return the tile boxes in reading order."""
    frame.locator("#rc-imageselect-target td").first.wait_for(state="visible", timeout=15000)
    time.sleep(1.5)                                        # tiles fade in
    head = frame.locator(".rc-imageselect-instructions").bounding_box()
    grid = frame.locator("#rc-imageselect-target").bounding_box()
    tiles = [b for b in (td.bounding_box() for td in frame.locator("#rc-imageselect-target td").all()) if b]
    x0, y0 = min(head["x"], grid["x"]), head["y"]
    clip = {"x": x0, "y": y0, "width": max(head["x"] + head["width"], grid["x"] + grid["width"]) - x0,
            "height": grid["y"] + grid["height"] - y0}
    task = frame.locator(".rc-imageselect-instructions").inner_text().replace("\n", " ").strip()
    SHOT.write_bytes(_numbered(page.screenshot(clip=clip), tiles, clip))
    _state("waiting", shot=str(SHOT), task=task, tiles=len(tiles),
           dynamic="none left" in task.lower())
    return tiles


def _click(page, box: dict) -> None:
    x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
    page.mouse.move(x, y, steps=random.randint(8, 16))
    time.sleep(random.uniform(0.1, 0.3))
    page.mouse.click(x, y)
    time.sleep(random.uniform(0.3, 0.7))


def _await_answer() -> list[str] | None:
    deadline = time.time() + WAIT_ANSWER_S
    while time.time() < deadline:
        if ANSWER.exists():
            words = ANSWER.read_text().replace(",", " ").split()
            ANSWER.unlink(missing_ok=True)
            return words
        time.sleep(0.5)
    return None


def start(punish_url: str) -> int:
    import slider
    from playwright.sync_api import sync_playwright

    DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for f in (SHOT, ANSWER):
        f.unlink(missing_ok=True)
    _state("starting")
    data = json.loads(CRED.read_text())
    cookies: dict[str, str] = data.get("cookies", {})
    xvfb, display = slider._start_xvfb()
    env = dict(os.environ, **({"DISPLAY": display} if display else {}))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=False, env=env,
                                        args=["--disable-blink-features=AutomationControlled", "--no-first-run"])
            try:
                ctx = browser.new_context(viewport={"width": 1280, "height": 860}, locale="en-US")
                ctx.add_cookies([{"name": k, "value": v, "domain": ".aliexpress.com", "path": "/"}
                                 for k, v in cookies.items()])
                page = ctx.new_page()
                page.goto(punish_url, wait_until="networkidle", timeout=60000)
                # A pass sets a NEW x5sec; the one from an earlier pass is in the cookies already.
                old = cookies.get("x5sec")
                passed = lambda: any(c["name"] == "x5sec" and c["value"] != old for c in ctx.cookies())
                slider._click_recaptcha(page, ctx)
                if passed():
                    return _finish(ctx, data, cookies)
                while True:
                    frame = _challenge(page)
                    if not frame:
                        time.sleep(3)
                        if passed():
                            return _finish(ctx, data, cookies)
                        _state("failed", why="no image challenge on the page")
                        return 1
                    tiles = _snapshot(page, frame)
                    words = _await_answer()
                    if words is None:
                        _state("failed", why="no answer within 15 min")
                        return 1
                    _state("clicking")
                    for w in words:
                        if w.isdigit() and 1 <= int(w) <= len(tiles):
                            _click(page, tiles[int(w) - 1])
                    dynamic = "none left" in (_read_state().get("task") or "").lower()
                    if dynamic and any(w.isdigit() for w in words):
                        time.sleep(4)                      # new tiles fade in: Florian looks again
                        continue
                    vb = frame.locator("#recaptcha-verify-button").bounding_box()
                    if vb:
                        _click(page, vb)
                    for _ in range(20):
                        time.sleep(0.5)
                        if passed():
                            return _finish(ctx, data, cookies)
                    # still here: wrong tiles or a further round; the loop takes a new picture
            finally:
                browser.close()
    finally:
        if xvfb:
            xvfb.terminate()


def _finish(ctx, data: dict, cookies: dict) -> int:
    time.sleep(1.0)
    for c in ctx.cookies():
        if "aliexpress" in c["domain"]:
            cookies[c["name"]] = c["value"]
    data["cookies"] = cookies
    private.write_json(CRED, data)
    BOT_FILE.unlink(missing_ok=True)                       # passed: the pause is over
    _state("passed")
    return 0


def answer(words: list[str], timeout_s: float = 60) -> int:
    st = _read_state()
    if st.get("status") != "waiting" or not _alive(st.get("pid")):
        print(f"no check is waiting for an answer (status: {st.get('status') or 'none'})")
        return 1
    since = st["at"]
    ANSWER.write_text(" ".join(words))
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(0.5)
        st = _read_state()
        if st.get("at", 0) > since and st.get("status") in ("waiting", "passed", "failed"):
            break
    print(_describe(st))
    return 0 if st.get("status") in ("waiting", "passed") else 1


def _describe(st: dict) -> str:
    s = st.get("status") or "none"
    if s == "waiting":
        return (f"waiting: task \"{st.get('task')}\", {st.get('tiles')} tiles"
                + (" (new tiles appear after clicks: 'v' when none are left)" if st.get("dynamic") else "")
                + f"\npicture: {st.get('shot')}")
    if s == "failed":
        return f"failed: {st.get('why')}"
    return s


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "start":
        url = argv[1] if len(argv) > 1 else (json.loads(BOT_FILE.read_text()).get("url") if BOT_FILE.exists() else "")
        if not url:
            print("no punish URL known (pass it, or wait for the next bot check)")
            return 1
        return start(url)
    if cmd == "answer":
        return answer(argv[1:])
    print(_describe(_read_state()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
