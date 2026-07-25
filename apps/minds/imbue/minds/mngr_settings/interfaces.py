from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from typing import Any

import tomlkit

from imbue.imbue_common.mutable_model import MutableModel


class MindsSettingsStoreInterface(MutableModel, ABC):
    """Serialized read/modify/write access to one profile's mngr settings.toml."""

    @abstractmethod
    def read(self) -> dict[str, Any]:
        """Return the parsed settings file contents, or an empty dict when the file does not exist."""

    @abstractmethod
    def update(self, mutate_document: Callable[[tomlkit.TOMLDocument], bool]) -> bool:
        """Apply a mutation under the store's inter-process write lock.

        ``mutate_document`` receives the current document (empty when the file does not exist) and returns whether its changes need to be written.
        The document is written back atomically iff the callback returns True, and that flag is returned so callers know whether observers need a bounce.
        """
