#!/usr/bin/env python3
"""Take the AliExpress cookies from a Chrome profile into the credential file this server reads.

Usage:
  import_profile.py [profile-dir]          read the profile, write the credentials
  import_profile.py --ask [profile-dir]    first ask the user to log in / pass the bot check in a
                                           real Chrome (ClaudeVoice remote-browser card, waits up to
                                           30 min), then import

The profile (default ~/.mcp-credentials/aliexpress-profile) is the one remote-browser.mjs fills.
Chrome only writes cookies to disk on a clean close, which remote-browser does. Prints cookie names
and how long the login cookies live, never values.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import private

CRED = Path(os.environ.get("ALIEXPRESS_CREDENTIALS", "~/.mcp-credentials/aliexpress.json")).expanduser()
PROFILE = Path("~/.mcp-credentials/aliexpress-profile").expanduser()
REMOTE_BROWSER = Path(os.environ.get("REMOTE_BROWSER", "~/repos/claude-code-voice/bin/remote-browser.mjs")).expanduser()
LOGIN = ("xman_t", "xman_us_t", "sgcookie", "havana_tgc")


def _key() -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    # Chrome with --password-store=basic encrypts with the fixed password "peanuts".
    return PBKDF2HMAC(hashes.SHA1(), 16, b"saltysalt", 1).derive(b"peanuts")


def _decrypt(key: bytes, enc: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if enc[:3] not in (b"v10", b"v11"):
        return ""
    try:
        d = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        p = d.update(enc[3:]) + d.finalize()
        p = p[:-p[-1]]
    except (ValueError, IndexError):  # truncated or foreign value: skip this cookie
        return ""
    if len(p) > 32 and not p[:32].isascii():  # newer Chrome puts a 32-byte hash in front
        p = p[32:]
    return p.decode("utf8", "replace")


def read_profile(profile: Path) -> tuple[dict[str, str], dict[str, float]]:
    """(cookies, days left per cookie) for aliexpress.com from a Chrome profile."""
    src = profile / "Default" / "Cookies"
    if not src.is_file():
        raise FileNotFoundError(f"no cookie database in {profile} (log in first: --ask)")
    key = _key()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "c.db"
        shutil.copy(src, db_path)
        db = sqlite3.connect(db_path)
        cookies, left = {}, {}
        for name, val, enc, exp in db.execute(
                "select name, value, encrypted_value, expires_utc from cookies where host_key like '%aliexpress%'"):
            v = val or _decrypt(key, enc)
            if v:
                cookies[name] = v
                left[name] = ((exp / 1e6 - 11644473600 - time.time()) / 86400) if exp else 0.0
        db.close()
    return cookies, left


def main(argv: list[str]) -> int:
    ask = "--ask" in argv
    args = [a for a in argv if not a.startswith("--")]
    profile = Path(args[0]).expanduser() if args else PROFILE
    if ask:
        if not REMOTE_BROWSER.is_file():
            print(f"{REMOTE_BROWSER} not found (set REMOTE_BROWSER)", file=sys.stderr)
            return 1
        r = subprocess.run(["node", str(REMOTE_BROWSER), "--label", "AliExpress: einloggen / Bot-Check lösen",
                            "--url", "https://www.aliexpress.com", "--profile", str(profile), "--minutes", "30"])
        if r.returncode != 0:
            print("remote browser cancelled or expired, nothing imported", file=sys.stderr)
            return 1
    try:
        cookies, left = read_profile(profile)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    login = [n for n in LOGIN if n in cookies]
    if not login:
        print("No login cookie in the profile: log in first (use --ask).", file=sys.stderr)
        return 1
    private.write_json(CRED, {"cookies": cookies})
    (CRED.parent / "aliexpress-botcheck.json").unlink(missing_ok=True)  # a fresh login ends the pause
    print(f"{len(cookies)} cookies written to {CRED}: " + ", ".join(sorted(cookies)))
    print("login cookies: " + ", ".join(f"{n} ({left[n]:.0f} days)" for n in login))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
