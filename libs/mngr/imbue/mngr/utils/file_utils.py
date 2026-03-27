import os
import stat
import tempfile
from pathlib import Path


def atomic_write(path: Path, content: str) -> None:
    """Write content to a file atomically using a temp file and rename.

    Writes to a temporary file in the same directory, flushes to disk with
    fsync, then atomically replaces the target file. This ensures readers
    never see a partially-written file, even after power loss.

    If the target file already exists, its permissions are preserved on the
    new file. Otherwise the file is created with default permissions (0600).

    The caller is responsible for catching OSError if the write fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Capture existing permissions before overwriting
    existing_mode: int | None = None
    try:
        existing_mode = path.stat().st_mode
    except OSError:
        pass

    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
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
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
