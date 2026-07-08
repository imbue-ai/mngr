"""The contract every on-disk data-format migration for the plugin implements.

The plugin persists all of its state under ``<latchkey_directory>/mngr_latchkey/``
(``Latchkey.plugin_data_dir``). When the shape of that on-disk state changes in a
way that is not forward/backward compatible, the change is expressed as a
:class:`DataFormatMigration` rather than as ad-hoc repair code sprinkled through
the readers. Each migration knows how to move the data from the version just
below its own up to its own (``apply_up``) and back down again (``apply_down``);
the runner in :mod:`imbue.mngr_latchkey.migrations.runner` sequences them against
the version recorded in the ``data-format-version`` file.
"""

from abc import ABC
from abc import abstractmethod
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel


class LatchkeyMigrationError(Exception):
    """Raised when a data-format migration cannot be sequenced or applied."""


class DataFormatMigration(MutableModel, ABC):
    """One reversible migration of the plugin's on-disk data format."""

    version: int = Field(
        frozen=True,
        gt=0,
        description="The data-format version reached once this migration is applied 'up' (consecutive from 1).",
    )

    @abstractmethod
    def apply_up(self, plugin_data_dir: Path) -> None:
        """Migrate the data under ``plugin_data_dir`` up from ``version - 1`` to :attr:`version`."""

    @abstractmethod
    def apply_down(self, plugin_data_dir: Path) -> None:
        """Revert the data under ``plugin_data_dir`` down from :attr:`version` to ``version - 1``."""
