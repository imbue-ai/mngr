"""Materializes the workspace profile snapshot into each workspace (spec section 8).

The snapshot (``data/.state/profile/account.json``) is the read-only copy of the account-keyed
settings a workspace's apps consume, written only by a client outside the workspace. It names
the workspace's account and owner, and carries the owning account's keybindings and whether
that account has experimental features on (the settings workspace apps read). The desktop
writes it on create (right after the welcome chat is seeded), on every sync pass in which a
running workspace's resolved snapshot differs from the one last written (a content hash kept per
workspace under the install), and on an association change.

The timezone rides the create-time write: the creating client writes
``data/.state/user_timezone`` with this device's zone exactly once, when the file does not
exist (spec section 8.4), and the template's bootstrap applies it at boot.
"""

import hashlib
import json
import threading
import time
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.account_settings_store import AccountSettingsStore
from imbue.minds.desktop_client.agent_address import build_agent_address
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.session_store import get_owning_account_id_for_workspace
from imbue.minds.desktop_client.settings.resolve import is_plan_opted_into_experimental_features
from imbue.minds.desktop_client.settings.resolve import resolve_experimental_features_opt_in
from imbue.minds.desktop_client.settings.resolve import resolve_keybindings
from imbue.minds.desktop_client.settings.storage import load_settings_context
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

# Reading a plan is a connector round trip, so it runs far less often than a sync pass.
_DEFAULT_PLAN_READ_INTERVAL_SECONDS: Final[float] = 300.0


@pure
def build_profile_snapshot_document(
    account_id: str,
    device_id: str,
    written_at: datetime,
    # The setting keys the template consumes (workspace_profile_contract.json pins them
    # cross-repo): the account's keybinding assignments, and whether experimental features are on.
    keybindings: dict[str, dict[str, tuple[str, ...]]],
    experimental_features_enabled: bool,
) -> dict[str, object]:
    """The snapshot document for a workspace owned by ``account_id`` (``local`` when unassociated).

    The owner's user id is the account id: a workspace is associated with at most one
    account, and that account's user is its owner.
    """
    return {
        "snapshot_format": SNAPSHOT_FORMAT,
        "account_id": account_id,
        "owner_user_id": account_id,
        "written_at": written_at.isoformat(),
        "written_by": device_id,
        "keybindings": {
            app: {action: list(chords) for action, chords in by_action.items()}
            for app, by_action in keybindings.items()
        },
        "experimental_features_enabled": experimental_features_enabled,
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


def read_account_plan_name(cli: ImbueCloudCli, email: str) -> str | None:
    """The account's plan name, or None when the connector cannot say."""
    try:
        info = cli.get_account_info(email)
    except ImbueCloudCliError as e:
        logger.debug("Could not read the plan of {} for the workspace profile: {}", email, e)
        return None
    return str(info.get("plan_name") or "") or None


class _PlanDefaultRead(FrozenModel):
    """What one account's plan last gave it, and when its plan was last tried."""

    is_opted_in: bool | None = Field(
        description="Whether the account's plan opts it into experimental features; None while no read has answered"
    )
    tried_at: float = Field(description="When its plan was last tried, on the monotonic clock")


class WorkspaceProfileWriter(MutableModel):
    """Writes the profile snapshot (and the create-time timezone) into workspaces over ``mngr exec``."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    mngr_caller: MngrCaller = Field(frozen=True, description="Runs the in-workspace writes")
    backend_resolver: BackendResolverInterface = Field(frozen=True, description="Places each workspace's host")
    session_store: MultiAccountSessionStore | None = Field(
        frozen=True, description="Resolves each workspace's owning account; None keys everything to the device"
    )
    settings_store: AccountSettingsStore | None = Field(
        frozen=True,
        description=(
            "Answers each account's keybindings and experimental features answer for the snapshot; "
            "None resolves the declared defaults"
        ),
    )
    device_id: str = Field(frozen=True, description="This install's device id, the snapshot's written_by")
    hashes_dir: Path = Field(
        frozen=True, description="Directory holding one <agent_id> file per workspace: the last-written content hash"
    )
    read_plan_name: Callable[[str], str | None] = Field(
        frozen=True, description="An account email's plan name; None when the connector cannot say"
    )
    plan_read_interval_seconds: float = Field(
        default=_DEFAULT_PLAN_READ_INTERVAL_SECONDS,
        frozen=True,
        description="How long what an account's plan gave it stands before the plan is read again",
    )

    _plan_default_read_by_email: dict[str, _PlanDefaultRead] = PrivateAttr(default_factory=dict)
    _plan_read_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def _hash_path(self, agent_id: AgentId) -> Path:
        return self.hashes_dir / str(agent_id)

    def read_experimental_features_plan_default(self, email: str) -> bool | None:
        """Whether the account's plan opts it into experimental features; None when that cannot be said yet.

        A failed read keeps what the plan last gave, so a connector outage never moves it, and counts
        against the interval like any other, so an outage costs no more reads. The sync pass and the
        Settings route both call this, so a caller behind a running read waits for its answer.
        """
        with self._plan_read_lock:
            previous = self._plan_default_read_by_email.get(email)
            now = time.monotonic()
            if previous is not None and now - previous.tried_at < self.plan_read_interval_seconds:
                return previous.is_opted_in
            plan_name = self.read_plan_name(email)
            if plan_name is not None:
                is_opted_in = is_plan_opted_into_experimental_features(plan_name)
            else:
                is_opted_in = None if previous is None else previous.is_opted_in
            self._plan_default_read_by_email[email] = _PlanDefaultRead(is_opted_in=is_opted_in, tried_at=now)
            return is_opted_in

    def _are_experimental_features_enabled(self, agent_id: AgentId) -> bool | None:
        """Whether the workspace's owning account has experimental features on; None when that cannot be said yet.

        The account's own answer wins, and needs no plan. Without one, a workspace with no owning account
        is opted out, and one whose owner has no readable account here (signed out on this device, or the
        account list failed) cannot be read, which is not the same answer.
        """
        account_id = get_owning_account_id_for_workspace(
            session_store=self.session_store, workspace_any_id=str(agent_id)
        )
        answer = resolve_experimental_features_opt_in(load_settings_context(None, self.settings_store, account_id))
        if answer is not None:
            return answer
        if self.session_store is None or self.session_store.get_owner_user_id_for_workspace(str(agent_id)) is None:
            return False
        account = self.session_store.get_account_for_workspace(str(agent_id))
        if account is None:
            return None
        return self.read_experimental_features_plan_default(str(account.email))

    def _resolve_snapshot_document(self, agent_id: AgentId, experimental_features_enabled: bool) -> dict[str, object]:
        account_id = get_owning_account_id_for_workspace(
            session_store=self.session_store, workspace_any_id=str(agent_id)
        )
        settings_context = load_settings_context(None, self.settings_store, account_id)
        return build_profile_snapshot_document(
            account_id,
            self.device_id,
            datetime.now(timezone.utc),
            resolve_keybindings(settings_context),
            experimental_features_enabled,
        )

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
        fetch-at-boot path also needed the desktop reachable). An owner with no answer of their own
        whose plan cannot be read yet counts as opted out until a sync pass can read it.
        """
        document = self._resolve_snapshot_document(agent_id, self._are_experimental_features_enabled(agent_id) is True)
        clauses = [atomic_write_clause(WORKSPACE_PROFILE_SNAPSHOT_PATH, json.dumps(document, indent=2), "tmp_profile")]
        if timezone_name:
            clauses.append(_timezone_once_clause(timezone_name))
        return self._exec_write(agent_id, clauses, document)

    def sync_profiles(self) -> None:
        """One pass: rewrite the snapshot of every running workspace whose resolved content moved.

        A workspace never written to (no stored hash) is written too, which is how workspaces
        created before this build get their first snapshot. A failed write keeps the previous
        hash and retries next pass. A workspace that already has a snapshot waits while its owner has
        no answer of their own and their plan cannot be read and never has been, so neither a connector
        outage nor a session gone from this device turns experimental features off.
        """
        for agent_id, host_state in self.backend_resolver.list_active_workspace_host_states().items():
            # An unknown host state counts as reachable, per the resolver's contract; the
            # exec's --no-start keeps a stopped workspace stopped either way.
            if not is_workspace_readable(host_state):
                continue
            experimental_features_enabled = self._are_experimental_features_enabled(agent_id)
            stored_hash = self._stored_hash(agent_id)
            if experimental_features_enabled is None and stored_hash:
                continue
            document = self._resolve_snapshot_document(agent_id, experimental_features_enabled is True)
            if profile_snapshot_content_hash(document) == stored_hash:
                continue
            clauses = [
                atomic_write_clause(WORKSPACE_PROFILE_SNAPSHOT_PATH, json.dumps(document, indent=2), "tmp_profile")
            ]
            self._exec_write(agent_id, clauses, document)
