"""TOML config file read/write utilities.

These are general-purpose helpers for reading and writing TOML config
files with tomlkit (which preserves formatting and comments). Used by
the CLI config command, plugin management, and the install wizard.
"""

from collections.abc import MutableMapping
from pathlib import Path
from typing import Any
from typing import cast

import tomlkit

from imbue.mngr.errors import ConfigStructureError
from imbue.mngr.utils.file_utils import atomic_write


def load_config_file_tomlkit(path: Path) -> tomlkit.TOMLDocument:
    """Load a TOML config file using tomlkit for preservation of formatting."""
    if not path.exists():
        return tomlkit.document()
    with open(path) as f:
        return tomlkit.load(f)


def save_config_file(path: Path, doc: tomlkit.TOMLDocument) -> None:
    """Save a TOML config file atomically."""
    atomic_write(path, tomlkit.dumps(doc))


def set_nested_value(doc: tomlkit.TOMLDocument, key_path: str, value: Any) -> None:
    """Set a value in nested tomlkit document using dot-separated key path.

    Works with tomlkit's TOMLDocument and Table types, which both behave like
    MutableMapping at runtime even though their type stubs don't perfectly reflect this.
    """
    keys = key_path.split(".")
    current: MutableMapping[str, Any] = doc
    for key in keys[:-1]:
        if key not in current:
            current[key] = tomlkit.table()
        next_val = current[key]
        if not isinstance(next_val, dict):
            raise ConfigStructureError(f"Cannot set nested key: {key} is not a table")
        current = cast(MutableMapping[str, Any], next_val)
    current[keys[-1]] = value


def set_plugin_enabled(name: str, *, is_enabled: bool, config_path: Path) -> None:
    """Set a plugin's enabled state in a config file.

    Loads the TOML, sets ``plugins.<name>.enabled``, and saves.
    Used by ``mngr plugin enable/disable`` and the install wizard.
    """
    doc = load_config_file_tomlkit(config_path)
    set_nested_value(doc, f"plugins.{name}.enabled", is_enabled)
    save_config_file(config_path, doc)
