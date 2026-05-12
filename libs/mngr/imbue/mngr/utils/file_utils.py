import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger


def read_json_dict(path: Path) -> dict[str, Any]:
    """Read ``path`` as a JSON object dict, returning ``{}`` if missing/malformed/non-dict.

    The "safe read" pattern several plugins want when consulting an optional
    user-managed config file like ``.claude/settings.json``: a typo in the
    file shouldn't break agent provisioning. Missing file -> ``{}``.
    Unparseable JSON -> log a warning and ``{}``. Non-object JSON (top-level
    list, string, etc.) -> ``{}``.

    For a host-aware variant that reads via ``OnlineHostInterface``, see
    ``mngr.hosts.host.read_json_dict_via_host``.
    """
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        logger.warning("Could not parse {} as JSON ({}); treating as empty.", path, e)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def atomic_write(path: Path, content: str) -> None:
    """Write content to a file atomically using a temp file and rename.

    Writes to a temporary file in the same directory, flushes to disk with
    fsync, then atomically replaces the target file. This ensures readers
    never see a partially-written file, even after power loss.

    If the target path is a symlink, the write goes through to the symlink's
    target so the link is preserved.

    If the target file already exists, its permissions are preserved on the
    new file. Otherwise the file is created with default permissions (0600).

    The caller is responsible for catching OSError if the write fails.
    """
    # Ensure the parent of the original path exists (for new non-symlink files).
    path.parent.mkdir(parents=True, exist_ok=True)

    # Resolve symlinks so os.replace overwrites the target file rather than
    # replacing the symlink itself with a regular file.
    resolved = path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)

    # Capture existing permissions before overwriting
    existing_mode: int | None = None
    try:
        existing_mode = resolved.stat().st_mode
    except OSError:
        pass

    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=resolved.parent,
        suffix=".tmp",
        delete=False,
    ) as tmp_file:
        tmp_file.write(content)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
        tmp_path = Path(tmp_file.name)

    try:
        if existing_mode is not None:
            os.chmod(tmp_path, stat.S_IMODE(existing_mode))
        os.replace(tmp_path, resolved)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
