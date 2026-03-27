"""Shared environment variable utilities for the mng_schedule plugin."""

import os
import shlex
from collections.abc import Sequence
from pathlib import Path

from loguru import logger

from imbue.imbue_common.pure import pure


@pure
def shell_quote_env_value(value: str) -> str:
    """Quote a value for safe use in a bash-sourced .env file.

    Uses shlex.quote to wrap the value in single quotes with proper
    escaping, so that bash source handles spaces and special characters
    correctly.
    """
    return shlex.quote(value)


def collect_env_lines(
    pass_env: Sequence[str] = (),
    env_files: Sequence[Path] = (),
) -> list[str]:
    """Collect environment variable lines from multiple sources.

    Sources are merged in order of increasing precedence:
    1. User-specified --env-file entries (in order)
    2. User-specified --pass-env variables from the current process environment

    Values from --pass-env are shell-quoted for safe use in bash-sourced
    .env files. Values from --env-file are passed through as-is (the
    user is responsible for correct quoting in their env files).

    Returns a list of lines in KEY=VALUE format (may also include comments
    and blank lines from env files).
    """
    env_lines: list[str] = []

    for env_file_path in env_files:
        env_lines.extend(env_file_path.read_text().splitlines())
        logger.info("Including env file {}", env_file_path)

    for var_name in pass_env:
        value = os.environ.get(var_name)
        if value is not None:
            env_lines.append(f"{var_name}={shell_quote_env_value(value)}")
            logger.debug("Passing through env var {}", var_name)
        else:
            logger.warning("Environment variable '{}' not set in current environment, skipping", var_name)

    return env_lines
