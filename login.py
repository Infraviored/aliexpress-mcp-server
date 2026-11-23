#!/usr/bin/env python3
"""Log in to AliExpress by itself, with the Chrome profile that already has the account and its password.

The profile (~/.mcp-credentials/aliexpress-profile, the one import_profile.py reads) shows the sign-in
dialog with the e-mail and the saved password filled in; only the click on the sign-in button was ever
missing. This opens that profile in a real Chrome on a virtual screen, clicks it, waits for the login
cookies and imports them (same file the MCP reads). The user is needed only when this fails
(wrong password, a code to type, a bot check the image solver cannot take): the exit code is 1 then.

Usage:
  login.py            log in if needed, import the cookies
  login.py --check    ask AliExpress whether the credential file's session is alive (exit 0/1)
"""
import sys
import time
from pathlib import Path

import import_profile

PROFILE = import_profile.PROFILE
SHOT = Path("~/.mcp-credentials/login-last.png").expanduser()
HOME = "https://www.aliexpress.com/"
SIGN_IN = ("Anmelden", "Sign in", "Log in", "Login")
FIELD = "input[type=password]"


def _page_logged_in(page) -> bool:
    """The header greets a member ("Hallo, <name>") and offers "Anmelden / Registrieren" to a guest."""
    try:
        return page.get_by_text("Anmelden / Registrieren").count() == 0 and page.get_by_text("Hallo,").count() > 0
    except Exception:
        return False


def saved_password() -> str:
    """The password Chrome saved for aliexpress in this profile (never printed, only typed into the form)."""
    import shutil
    import sqlite3
    import tempfile
    src = PROFILE / "Default" / "Login Data"
    if not src.is_file():
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "l.db"
        shutil.copy(src, db_path)
        db = sqlite3.connect(db_path)
        rows = db.execute("select password_value from logins where origin_url like '%aliexpress%' "
                          "order by date_last_used desc").fetchall()
        db.close()
    key = import_profile._key()
    for (enc,) in rows:
        pw = import_profile._decrypt(key, enc)
        if pw:
            return pw
    return ""


def _click_button(page, names: tuple[str, ...], roles=("button", "link"), exact=False) -> bool:
    for n in names:
        for role in roles:
            loc = page.get_by_role(role, name=n, exact=exact)
            if loc.count():
                try:
                    loc.first.click(timeout=4000)
                    return True
                except Exception:       # covered or detached: try the next label
                    continue
    return False


def login(timeout_s: float = 90) -> int:
    import slider
    from playwright.sync_api import sync_playwright

    xvfb, display = slider._start_xvfb()
    env = {"DISPLAY": display} if display else {}
    import os
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE), channel="chrome", headless=False, env=dict(os.environ, **env),
                viewport={"width": 1280, "height": 860}, locale="de-DE",
                args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                      "--password-store=basic"])
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
                deadline = time.time() + timeout_s
                clicked_open = clicked_submit = False
                while time.time() < deadline:
                    page.wait_for_timeout(1500)
                    if _page_logged_in(page):
                        return 0                               # the finally below closes Chrome: cookies get written
                    field = page.locator(FIELD)
                    if field.count() and field.first.is_visible():
                        if not field.first.input_value():
                            pw = saved_password()
                            if not pw:
                                print("no saved password in the profile: log in once by hand (import_profile.py --ask)",
                                      file=sys.stderr)
                                return 1
                            field.first.fill(pw)
                            del pw
                        if field.first.input_value() and not clicked_submit:
                            clicked_submit = _click_button(page, ("Anmelden",), roles=("button",), exact=True)
                        continue
                    # No password field yet: the header link opens a menu whose big button opens the dialog.
                    if not _click_button(page, ("Anmelden",), roles=("button",), exact=True):
                        if not clicked_open or time.time() > deadline - timeout_s / 2:
                            clicked_open = _click_button(page, SIGN_IN) or clicked_open
                page.screenshot(path=str(SHOT))
                print(f"login not completed in {timeout_s:.0f}s; screenshot in {SHOT}", file=sys.stderr)
                return 1
            finally:
                ctx.close()                                    # Chrome writes its cookies on a clean close
    finally:
        if xvfb:
            xvfb.terminate()


def main(argv: list[str]) -> int:
    if "--check" in argv:
        import aliexpress_mcp_server as m
        ok = m.session_alive(force=True)
        print("logged in" if ok else "not logged in")
        return 0 if ok else 1
    if login() != 0:
        return 1
    return import_profile.main([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
