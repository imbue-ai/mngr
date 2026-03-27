"""Env file loader for use in Modal deployments.

Uses python-dotenv for parsing to ensure consistent behavior with the
mng framework's own env file handling (imbue.mng.utils.env_utils).

This module must NOT import from imbue.* packages -- it is used by
cron_runner.py which runs standalone on Modal via `modal deploy`.
"""

import os
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values


def load_env_file(env_file_path: Path) -> None:
    """Load environment variables from a .env file into os.environ.

    Uses python-dotenv for parsing, which handles comments, blank lines,
    quoted values, the 'export' prefix, and multiline values. This ensures
    consistent parsing with the mng framework's --host-env-file handling.
    """
    if not env_file_path.exists():
        return
    content = env_file_path.read_text()
    parsed = dotenv_values(stream=StringIO(content))
    for key, value in parsed.items():
        if value is not None:
            os.environ[key] = value
