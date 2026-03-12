from __future__ import annotations

import shlex
import tomllib
from pathlib import Path
from typing import TypeVar

from loguru import logger
from pydantic import BaseModel

from imbue.imbue_common.logging import log_span
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng_llm.data_types import LlmSettings

SETTINGS_FILENAME = "minds.toml"

_T = TypeVar("_T", bound=BaseModel)


def load_from_path(settings_path: Path, settings_class: type[_T]) -> _T:
    """Load settings from a local file, returning defaults if missing."""
    if not settings_path.exists():
        logger.debug("No settings file at {}, using defaults", settings_path)
        return settings_class()

    with log_span("Loading settings from {}", settings_path):
        raw = tomllib.loads(settings_path.read_text())
        return settings_class.model_validate(raw)


def load_from_host(host: OnlineHostInterface, work_dir: Path, settings_class: type[_T]) -> _T:
    """Load settings from a host, returning defaults on any error."""
    settings_path = work_dir / SETTINGS_FILENAME
    quoted_path = shlex.quote(str(settings_path))

    check = host.execute_command(f"test -f {quoted_path}", timeout_seconds=15.0)
    if not check.success:
        logger.debug("No settings file at {}, using defaults", settings_path)
        return settings_class()

    with log_span("Loading settings from {}", settings_path):
        try:
            content = host.read_text_file(settings_path)
        except OSError as e:
            logger.warning("Failed to read settings file {}: {}", settings_path, e)
            return settings_class()

        try:
            raw = tomllib.loads(content)
        except tomllib.TOMLDecodeError as e:
            logger.warning("Failed to parse settings file {}: {}", settings_path, e)
            return settings_class()

        try:
            return settings_class.model_validate(raw)
        except ValueError as e:
            logger.warning("Failed to validate settings from {}: {}", settings_path, e)
            return settings_class()


def load_settings_from_path(settings_path: Path) -> LlmSettings:
    """Load LlmSettings from a local minds.toml file.

    Returns an LlmSettings with defaults for any missing values.
    If the file does not exist, returns all defaults.
    Raises tomllib.TOMLDecodeError if the file exists but has invalid TOML syntax.
    """
    return load_from_path(settings_path, LlmSettings)


def load_settings_from_host(
    host: OnlineHostInterface,
    work_dir: Path,
) -> LlmSettings:
    """Load LlmSettings from minds.toml in the agent's work directory.

    Returns an LlmSettings with defaults for any missing values.
    If the file does not exist or cannot be parsed, returns all defaults.
    """
    return load_from_host(host, work_dir, LlmSettings)
