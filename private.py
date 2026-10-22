"""Files that hold secrets or account state: written atomically, mode 600."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json(path: Path, obj: Any) -> None:
    """Replace `path` with `obj` as JSON; readable by the owner only, never half-written."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A temp file of its own: several server processes may write the same file.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
