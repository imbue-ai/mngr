from __future__ import annotations

from pathlib import Path

from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng_claude_mind.data_types import ClaudeMindSettings
from imbue.mng_llm.settings import SETTINGS_FILENAME as SETTINGS_FILENAME
from imbue.mng_llm.settings import load_from_host
from imbue.mng_llm.settings import load_from_path


def load_settings_from_path(settings_path: Path) -> ClaudeMindSettings:
    """Load ClaudeMindSettings from a local minds.toml file.

    Returns a ClaudeMindSettings with defaults for any missing values.
    If the file does not exist, returns all defaults.
    Raises tomllib.TOMLDecodeError if the file exists but has invalid TOML syntax.
    """
    return load_from_path(settings_path, ClaudeMindSettings)


def load_settings_from_host(
    host: OnlineHostInterface,
    work_dir: Path,
) -> ClaudeMindSettings:
    """Load ClaudeMindSettings from minds.toml in the agent's work directory.

    Returns a ClaudeMindSettings with defaults for any missing values.
    If the file does not exist or cannot be parsed, returns all defaults.
    """
    return load_from_host(host, work_dir, ClaudeMindSettings)
