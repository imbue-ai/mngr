"""The contract every migration of a host's permissions implements.

The plugin keeps each host's policy in ``latchkey_permissions.json`` under the
host's directory. When the shape of that policy changes in a way the readers
cannot absorb, the change is expressed as one :class:`PermissionsMigration`,
numbered consecutively from 1, and the runner in
:mod:`imbue.mngr_latchkey.migrations.runner` applies the ones a host's stamp
says it has not had yet.

A migration is a pure function of the parsed policy and of the desktop running
it: it is handed the
:class:`~imbue.mngr_latchkey.store.LatchkeyPermissionsConfig` as the version
below its own wrote it, and returns the config in its own version. The runner
does the reading, writing and stamping.

The stamp is written after the policy it describes, on this computer and on
the machine alike, so a failure between the two leaves a policy ahead of its
stamp and the migration runs once more. A migration must therefore leave a
policy already in its target shape alone.
"""

from abc import ABC
from abc import abstractmethod

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.devices import DesktopDeviceId
from imbue.mngr_latchkey.primitives import PermissionsFormatVersion
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig


class PermissionsMigrationError(LatchkeyError):
    """Raised when a host's policy cannot be brought to the format this build reads."""


class PermissionsFormatNewerError(PermissionsMigrationError):
    """Raised when a host's policy is in a format newer than this build knows, which only a newer build can read."""


class PermissionsMigrationContext(FrozenModel):
    """What a migration may know beyond the policy: the desktop running it."""

    device_id: DesktopDeviceId = Field(
        description=(
            "The desktop migrating the policy. A policy written before grants named a desktop was written by the "
            "only desktop there was, so what it granted is attributed to the one migrating it."
        )
    )


class PermissionsMigration(MutableModel, ABC):
    """One step of the permissions format: how a policy moves from the version below its own up to its own."""

    version: PermissionsFormatVersion = Field(
        frozen=True,
        description="The format version a policy is in once this migration has run; consecutive from 1.",
    )

    @abstractmethod
    def apply(
        self, permissions: LatchkeyPermissionsConfig, context: PermissionsMigrationContext
    ) -> LatchkeyPermissionsConfig:
        """Return ``permissions``, written in ``version - 1``, as :attr:`version` writes them; unchanged when already so.

        Raises:
            PermissionsMigrationError: when the policy cannot be rewritten.
        """
