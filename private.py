"""Files that hold secrets or account state: written atomically, mode 600."""

import json
import os
from pathlib import Path
from typing import Any


def write_json(path: Path, obj: Any) -> None:
    """Replace `path` with `obj` as JSON; readable by the owner only, never half-written."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
