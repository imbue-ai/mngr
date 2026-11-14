"""Why backups could not be set up for a newly created workspace.

Backup setup runs on a detached thread after a create, with nobody watching;
when it gives up, the notification feed says so and the workspace's Backups
page shows the failure (with its text behind See details) and offers to run
the same setup again. The setup request is kept with the failure for that
retry. Held in memory, like the notification feed entry that points at it.
"""

import threading
from enum import auto
from typing import assert_never

from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.mngr.primitives import AgentId


class BackupSetupFailureKind(UpperCaseStrEnum):
    """What kind of failure stopped a new workspace's backup setup."""

    # The account's Imbue Cloud plan has no room for another backup bucket.
    STORAGE_QUOTA = auto()
    # Anything else (most often a workspace that never became reachable).
    OTHER = auto()


class BackupSetupFailure(FrozenModel):
    """One workspace's last failed backup setup."""

    kind: BackupSetupFailureKind = Field(description="What kind of failure it was")
    detail: str = Field(description="The failure's own text, for See details")
    request: BackupSetupRequest = Field(description="The setup that failed, so it can be run again")


class BackupSetupFailureStore(MutableModel):
    """The last failed backup setup per workspace (written by the setup thread, read by request threads)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    failure_by_agent_id: dict[AgentId, BackupSetupFailure] = Field(
        default_factory=dict, description="The last failed setup per workspace"
    )
    lock: threading.Lock = Field(default_factory=threading.Lock, description="Guards failure_by_agent_id")

    def record(self, agent_id: AgentId, failure: BackupSetupFailure) -> None:
        with self.lock:
            self.failure_by_agent_id[agent_id] = failure

    def get(self, agent_id: AgentId) -> BackupSetupFailure | None:
        with self.lock:
            return self.failure_by_agent_id.get(agent_id)


@pure
def backup_setup_failure_body(kind: BackupSetupFailureKind) -> str:
    """The notification's body: what is true for the workspace, never the failure's raw text."""
    match kind:
        case BackupSetupFailureKind.STORAGE_QUOTA:
            return "Your Imbue Cloud plan has no room for this workspace's backups."
        case BackupSetupFailureKind.OTHER:
            return "Backups aren't set up for this workspace yet."
        case _ as unreachable:
            assert_never(unreachable)
