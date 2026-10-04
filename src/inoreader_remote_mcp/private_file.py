"""JSON files only the owner may read: the Inoreader tokens and the OAuth state.

Written atomically (temporary file, then `os.replace`): a container stopped mid-write must
leave either the old content or the new, never half a file — a lost refresh token means
going through the browser authorisation again.

The file is created `0600` from the start (`os.open` with a mode), not chmod-ed after the
fact: there is no window during which it is readable by others.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    # `os.open`'s mode is filtered by the umask; enforce it, in case the temporary file
    # pre-existed with looser permissions.
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
