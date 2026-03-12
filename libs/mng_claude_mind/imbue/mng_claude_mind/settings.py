from __future__ import annotations

import shlex
import tomllib
from pathlib import Path

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng_claude_mind.data_types import ClaudeMindSettings

SETTINGS_FILENAME = "minds.toml"


def load_settings_from_path(settings_path: Path) -> ClaudeMindSettings:
    """Load settings from a local minds.toml file.

    Returns a ClaudeMindSettings with defaults for any missing values.
    If the file does not exist, returns all defaults.
    Raises tomllib.TOMLDecodeError if the file exists but has invalid TOML syntax.
    """
    if not settings_path.exists():
        logger.debug("No settings file at {}, using defaults", settings_path)
        return ClaudeMindSettings()

    with log_span("Loading settings from {}", settings_path):
        raw = tomllib.loads(settings_path.read_text())
        return ClaudeMindSettings.model_validate(raw)


def load_settings_from_host(
    host: OnlineHostInterface,
    work_dir: Path,
) -> ClaudeMindSettings:
    """Load settings from minds.toml in the agent's work directory.

    Returns a ClaudeMindSettings with defaults for any missing values.
    If the file does not exist or cannot be parsed, returns all defaults.
    """
    settings_path = work_dir / SETTINGS_FILENAME
    quoted_path = shlex.quote(str(settings_path))

    check = host.execute_command(f"test -f {quoted_path}", timeout_seconds=15.0)
    if not check.success:
        logger.debug("No settings file at {}, using defaults", settings_path)
        return ClaudeMindSettings()

    with log_span("Loading settings from {}", settings_path):
        try:
            content = host.read_text_file(settings_path)
        except Exception as e:
            logger.warning("Failed to read settings file {}: {}", settings_path, e)
            return ClaudeMindSettings()

        try:
            raw = tomllib.loads(content)
        except tomllib.TOMLDecodeError as e:
            logger.warning("Failed to parse settings file {}: {}", settings_path, e)
            return ClaudeMindSettings()

        try:
            return ClaudeMindSettings.model_validate(raw)
        except Exception as e:
            logger.warning("Failed to validate settings from {}: {}", settings_path, e)
            return ClaudeMindSettings()
