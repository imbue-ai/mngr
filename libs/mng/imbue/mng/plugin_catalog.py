"""Catalog of recommended mng plugins.

This module defines which plugins are recommended for installation and
which are pre-selected by default in the install wizard.  It lives
outside the CLI layer so that tests and other consumers can import the
data without pulling in TUI dependencies.
"""

from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class RecommendedPlugin(FrozenModel):
    """A plugin available for selection in the install wizard."""

    package_name: str = Field(description="PyPI package name")
    description: str = Field(description="Human-readable description")
    is_preselected: bool = Field(default=False, description="Whether pre-selected by default")


# Descriptions sourced from each plugin's pyproject.toml.
RECOMMENDED_PLUGINS: Final[tuple[RecommendedPlugin, ...]] = (
    RecommendedPlugin(
        package_name="mng-opencode",
        description="OpenCode agent type plugin for mng",
    ),
    RecommendedPlugin(
        package_name="mng-pair",
        description="Pair command plugin for mng - continuous file sync between agent and local directory",
    ),
    RecommendedPlugin(
        package_name="mng-tutor",
        description="Interactive tutorial plugin for mng",
        is_preselected=True,
    ),
)
