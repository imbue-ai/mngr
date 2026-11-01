"""Materializes the workspace profile snapshot into each workspace (spec section 8).

The snapshot (``data/.state/profile/account.json``) is the read-only copy of the account-keyed
settings a workspace's apps consume, written only by a client outside the workspace. It names
the workspace's account and owner; it carries no setting keys yet (keybindings arrive with
phase 3C). The desktop writes it on create (right after the welcome chat is seeded), on every
sync pass in which a running workspace's resolved snapshot differs from the one last written
(a content hash kept per workspace under the install), and on an association change.

The timezone rides the create-time write: the creating client writes
``data/.state/user_timezone`` with this device's zone exactly once, when the file does not
exist (spec section 8.4), and the template's bootstrap applies it at boot.
"""

import hashlib
import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.agent_address import build_agent_address
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.session_store import get_owning_account_id_for_workspace
from imbue.minds.desktop_client.share_materials_injection import atomic_write_clause
from imbue.minds.desktop_client.workspace_update_state import is_workspace_readable
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.primitives import AgentId

# The snapshot and timezone paths inside a workspace, relative to its root. The template's
# ``workspace_profile`` library reads the snapshot; the contract fixture beside this module
# pins its format and keys cross-repo.
WORKSPACE_PROFILE_SNAPSHOT_PATH: Final[str] = "data/.state/profile/account.json"
USER_TIMEZONE_PATH: Final[str] = "data/.state/user_timezone"

# The snapshot shape this build writes; bumped only when a reader must tell shapes apart.
SNAPSHOT_FORMAT: Final[int] = 1

_WRITE_EXEC_TIMEOUT_SECONDS: Final[float] = 60.0


@pure
def build_profile_snapshot_document(account_id: str, device_id: str, written_at: datetime) -> dict[str, object]:
    """The snapshot document for a workspace owned by ``account_id`` (``local`` when unassociated).

    The owner's user id is the account id: a workspace is associated with at most one
    account, and that account's user is its owner. No setting keys yet (spec section 8.1).
    """
    return {
        "snapshot_format": SNAPSHOT_FORMAT,
        "account_id": account_id,
        "owner_user_id": account_id,
        "written_at": written_at.isoformat(),
        "written_by": device_id,
    }


@pure
def profile_snapshot_content_hash(document: dict[str, object]) -> str:
    """The hash deciding whether a workspace's snapshot must be rewritten.

    ``written_at`` is excluded: it changes on every write, and hashing it would make every
    pass rewrite every snapshot.
    """
    stable = {key: value for key, value in document.items() if key != "written_at"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode("utf-8")).hexdigest()


@pure
def _timezone_once_clause(timezone_name: str) -> str:
    """One shell clause writing the device's zone to the timezone file only when it does not exist."""
    encoded_write = atomic_write_clause(USER_TIMEZONE_PATH, timezone_name + "\n", "tmp_zone")
    return f"{{ test -f {USER_TIMEZONE_PATH} || {{ {encoded_write}; }}; }}"


class WorkspaceProfileWriter(MutableModel):
    """Writes the profile snapshot (and the create-time timezone) into workspaces over ``mngr exec``."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    mngr_caller: MngrCaller = Field(frozen=True, description="Runs the in-workspace writes")
    backend_resolver: BackendResolverInterface = Field(frozen=True, description="Places each workspace's host")
    session_store: MultiAccountSessionStore | None = Field(
        frozen=True, description="Resolves each workspace's owning account; None keys everything to the device"
    )
    device_id: str = Field(frozen=True, description="This install's device id, the snapshot's written_by")
    hashes_dir: Path = Field(
        frozen=True, description="Directory holding one <agent_id> file per workspace: the last-written content hash"
    )

    def _hash_path(self, agent_id: AgentId) -> Path:
        return self.hashes_dir / str(agent_id)

    def _resolve_snapshot_document(self, agent_id: AgentId) -> dict[str, object]:
        account_id = get_owning_account_id_for_workspace(
            session_store=self.session_store, workspace_any_id=str(agent_id)
        )
        return build_profile_snapshot_document(account_id, self.device_id, datetime.now(timezone.utc))

    def _stored_hash(self, agent_id: AgentId) -> str:
        try:
            return self._hash_path(agent_id).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""
        except OSError as e:
            logger.warning("Could not read the workspace profile hash for {}: {}", agent_id, e)
            return ""

    def _store_hash(self, agent_id: AgentId, content_hash: str) -> None:
        try:
            self.hashes_dir.mkdir(parents=True, exist_ok=True)
            self._hash_path(agent_id).write_text(content_hash, encoding="utf-8")
        except OSError as e:
            logger.warning("Could not store the workspace profile hash for {}: {}", agent_id, e)

    def mark_profile_stale(self, agent_id: AgentId) -> None:
        """Forget the last-written hash so the next pass rewrites the snapshot (an association changed)."""
        try:
            self._hash_path(agent_id).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Could not drop the workspace profile hash for {}: {}", agent_id, e)

    def _exec_write(self, agent_id: AgentId, clauses: list[str], document: dict[str, object]) -> bool:
        address = build_agent_address(agent_id, self.backend_resolver)
        result = self.mngr_caller.call(
            ["exec", address, " && ".join(clauses), "--no-start"], timeout=_WRITE_EXEC_TIMEOUT_SECONDS
        )
        if result.returncode != 0:
            logger.debug("Could not write the profile snapshot into {}: {}", agent_id, result.stderr.strip()[-300:])
            return False
        self._store_hash(agent_id, profile_snapshot_content_hash(document))
        return True

    def write_profile_for_create(self, agent_id: AgentId, timezone_name: str) -> bool:
        """The create-time write: the snapshot, plus the device's zone when no timezone file exists yet.

        Best-effort: a workspace the write cannot reach gets its snapshot from the next sync
        pass, and a timezone only if it is created again (an accepted loss -- the old
        fetch-at-boot path also needed the desktop reachable).
        """
        document = self._resolve_snapshot_document(agent_id)
        clauses = [atomic_write_clause(WORKSPACE_PROFILE_SNAPSHOT_PATH, json.dumps(document, indent=2), "tmp_profile")]
        if timezone_name:
            clauses.append(_timezone_once_clause(timezone_name))
        return self._exec_write(agent_id, clauses, document)

    def sync_profiles(self) -> None:
        """One pass: rewrite the snapshot of every running workspace whose resolved content moved.

        A workspace never written to (no stored hash) is written too, which is how workspaces
        created before this build get their first snapshot. A failed write keeps the previous
        hash and retries next pass.
        """
        for agent_id, host_state in self.backend_resolver.list_active_workspace_host_states().items():
            # An unknown host state counts as reachable, per the resolver's contract; the
            # exec's --no-start keeps a stopped workspace stopped either way.
            if not is_workspace_readable(host_state):
                continue
            document = self._resolve_snapshot_document(agent_id)
            if profile_snapshot_content_hash(document) == self._stored_hash(agent_id):
                continue
            clauses = [
                atomic_write_clause(WORKSPACE_PROFILE_SNAPSHOT_PATH, json.dumps(document, indent=2), "tmp_profile")
            ]
            self._exec_write(agent_id, clauses, document)
