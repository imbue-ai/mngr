"""Loader for MindsConfig.

Reads ``<data_dir>/config.toml`` (flat top-level TOML keys matching MindsConfig
field names), overlays per-field env var values using each field's ``alias``,
and returns a validated MindsConfig. Precedence: env > file > built-in default.
"""

import os
import tomllib
from pathlib import Path
from typing import Any

from imbue.minds.config.data_types import MindsConfig
from imbue.minds.errors import MindsConfigError

CONFIG_FILENAME = "config.toml"


def load_minds_config(data_dir: Path) -> MindsConfig:
    """Load MindsConfig from ``<data_dir>/config.toml`` with env var overrides.

    If the file is absent, the defaults baked into MindsConfig are used. Env
    vars listed as field aliases override any value from the file.

    Raises MindsConfigError if the TOML file exists but cannot be parsed or if
    validation fails (e.g. malformed URL).
    """
    merged: dict[str, Any] = _load_toml_file(data_dir / CONFIG_FILENAME)
    for field_name, field_info in MindsConfig.model_fields.items():
        env_var_name = field_info.alias
        if env_var_name is None:
            continue
        env_value = os.environ.get(env_var_name)
        if env_value is not None:
            merged[field_name] = env_value
    try:
        return MindsConfig.model_validate(merged)
    except ValueError as e:
        raise MindsConfigError("Failed to validate minds config: {}".format(e)) from e


def _load_toml_file(path: Path) -> dict[str, Any]:
    """Read a TOML file into a dict; return empty dict if the file is missing."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise MindsConfigError("Failed to parse {}: {}".format(path, e)) from e
