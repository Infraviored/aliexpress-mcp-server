#!/usr/bin/env python3
"""Turn pasted AliExpress cookies into the credential file this server reads.

Accepts any of:
  - a "Copy as cURL" command (Cookie taken from -H 'cookie: …' or -b '…')
  - a bare Cookie header value: "a=1; b=2"
  - JSON: {"cookies": {...}} or {...}

Usage: import_cookies.py <raw-file> [--keep]
Writes $ALIEXPRESS_CREDENTIALS (default ~/.mcp-credentials/aliexpress.json), mode 600,
and deletes the raw file unless --keep. Prints only cookie names, never values.
"""
import json
import os
import re
import sys
from pathlib import Path


def parse(raw: str) -> dict[str, str]:
    raw = raw.strip()
    try:
        data = json.loads(raw)
        return {str(k): str(v) for k, v in data.get("cookies", data).items()}
    except (json.JSONDecodeError, AttributeError):
        pass
    m = re.search(r"-H\s+(['\"])cookie:\s*(.*?)\1", raw, re.I | re.S) or re.search(
        r"(?:-b|--cookie)\s+(['\"])(.*?)\1", raw, re.S
    )
    header = m.group(2) if m else re.sub(r"^cookie:\s*", "", raw, flags=re.I)
    cookies = {}
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            cookies[name] = value
    return cookies


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1]).expanduser()
    cookies = parse(src.read_text())
    if not cookies:
        print("No cookies found in input.", file=sys.stderr)
        return 1
    out = Path(
        os.environ.get("ALIEXPRESS_CREDENTIALS", "~/.mcp-credentials/aliexpress.json")
    ).expanduser()
    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"cookies": cookies}, f)
    os.chmod(out, 0o600)
    if "--keep" not in sys.argv:
        src.unlink()
    print(f"Wrote {len(cookies)} cookies to {out}: {', '.join(sorted(cookies))}")
    missing = [k for k in ("_m_h5_tk", "xman_t") if k not in cookies]
    if missing:
        print(f"Warning: missing {', '.join(missing)} (HttpOnly? use Copy as cURL)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
