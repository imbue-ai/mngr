"""One-shot startup reconcile for orphaned Lima / Docker workspace hosts.

A workspace create that dies with the app (quit SIGTERMs the ``mngr create``
subprocess; a crash orphans it) leaves one of two things behind: a half-built
host that burns CPU forever (Lima workspaces run with idle shutdown disabled),
or a finished workspace whose account association was never recorded. This
module runs once per app startup, after discovery is available, and repairs
both -- driven by the ``workspace-id`` host label the create stamps on every
Lima / Docker host and the local pending-create-attempt records behind it.

Policy (see the lima-workspace-reliability decision log):

- A labeled host WITH a completed workspace (its ``system-services`` agent
  exists) and a pending record -> re-associate with the record's account (or
  leave private when the record carries none) and let the record hand off to
  discovery. With no record left, the workspace is simply adopted as a
  private workspace (discovery already lists it).
- A labeled half-built host (no ``system-services`` agent) is destroyed once
  its pending record is older than a 60-minute grace window (or gone
  entirely). The grace exists because after a CRASH the orphaned ``mngr
  create`` subprocess can still be mid-provisioning at the next startup. The
  record itself is kept: it backs the interrupted row's retry / dismiss.
- Hosts WITHOUT the label are never touched -- an agent-less one only gets a
  warning naming the manual cleanup command.
- Finally ``mngr gc`` (scoped to the lima + docker providers) sweeps stale
  FAILED / DESTROYED host records older than the provider's configured
  ``destroyed_host_persisted_seconds`` (mngr's 7-day default) -- minds envs
  never run gc otherwise, so the records would accumulate forever.

Everything mngr-facing goes through the CLI (``mngr list --hosts``,
``mngr destroy``, ``mngr gc``); all outcomes are log-only.
"""

import os
import threading
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import SYSTEM_SERVICES_AGENT_NAME
from imbue.minds.desktop_client.labeled_hosts import ListedHost
from imbue.minds.desktop_client.labeled_hosts import WORKSPACE_ID_LABELED_PROVIDER_NAMES
from imbue.minds.desktop_client.labeled_hosts import list_provider_hosts
from imbue.minds.desktop_client.mngr_command import run_mngr_to_completion
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRecord
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptStore
from imbue.minds.desktop_client.pending_create_attempts import WORKSPACE_ID_HOST_LABEL
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.errors import MngrCommandError
from imbue.minds.errors import WorkspaceSyncError

# The providers whose hosts the reconcile owns: exactly the ones whose hosts
# carry the ``workspace-id`` label (see ``labeled_hosts`` for the exclusion
# rationale).
RECONCILED_PROVIDER_NAMES: Final[tuple[str, ...]] = WORKSPACE_ID_LABELED_PROVIDER_NAMES

# How long a labeled half-built host is left alone after its create started.
# After a CRASH (not quit) the orphaned ``mngr create`` subprocess survives and
# can legitimately still be provisioning at the next app startup; destroying
# under it would waste the work and strand the subprocess mid-create.
HALF_BUILT_HOST_GRACE_SECONDS: Final[float] = 3600.0

# How long the reconcile waits for the first discovery snapshot before running
# anyway. The reconcile reads its own live inventory through the CLI, so a
# stalled discovery pipeline should delay it, not block it forever.
_DISCOVERY_WAIT_TIMEOUT_SECONDS: Final[float] = 300.0
_DISCOVERY_POLL_INTERVAL_SECONDS: Final[float] = 1.0

# ``mngr list --hosts`` runs a live provider discovery; ``mngr destroy`` tears
# down a VM; ``mngr gc`` fans out to every host record. All are one-shot
# startup work, so the ceilings are generous rather than tight.
_HOST_LIST_TIMEOUT_SECONDS: Final[float] = 120.0
_DESTROY_TIMEOUT_SECONDS: Final[float] = 300.0
_GC_TIMEOUT_SECONDS: Final[float] = 600.0


class PendingCreateAttemptDiscoverySweep(MutableModel):
    """Resolver on-change callback deleting DONE pending records once discovery confirms them.

    Registered on the backend resolver so the success-path record deletion
    happens exactly when the workspace first appears in a snapshot -- the
    create attempt row can then hand off to the real workspace row without flicker.
    Cheap on every other change: the store answers ``has_done_records`` from
    memory.
    """

    store: PendingCreateAttemptStore = Field(frozen=True, description="The pending-create-attempt record store")
    backend_resolver: BackendResolverInterface = Field(
        frozen=True, description="Resolver whose known-workspace set confirms a create attempt"
    )

    def __call__(self) -> None:
        if not self.store.has_done_records():
            return
        known_agent_id_strs = frozenset(str(aid) for aid in self.backend_resolver.list_known_workspace_ids())
        self.store.sweep_confirmed_records(known_agent_id_strs)


class StartupHostReconciler(MutableModel):
    """Runs the one-shot startup reconcile over Lima / Docker hosts."""

    backend_resolver: BackendResolverInterface = Field(
        frozen=True, description="Resolver used to wait for discovery and to wake the UI after adoptions"
    )
    agent_creator: AgentCreator = Field(
        frozen=True, description="Creator whose live in-flight create attempts must never be reconciled against"
    )
    pending_create_attempt_store: PendingCreateAttemptStore = Field(
        frozen=True, description="Pending-create-attempt records"
    )
    session_store: MultiAccountSessionStore | None = Field(
        frozen=True, description="Session store used to restore workspace<->account associations, when available"
    )
    mngr_binary: str = Field(frozen=True, description="mngr binary to shell out to")
    mngr_host_dir: Path = Field(frozen=True, description="MNGR_HOST_DIR for every mngr subprocess")
    concurrency_group: ConcurrencyGroup = Field(frozen=True, description="Parent group for mngr subprocesses")
    half_built_grace_seconds: float = Field(
        default=HALF_BUILT_HOST_GRACE_SECONDS,
        frozen=True,
        description="Grace window before a labeled half-built host is destroyed",
    )
    discovery_wait_timeout_seconds: float = Field(
        default=_DISCOVERY_WAIT_TIMEOUT_SECONDS,
        frozen=True,
        description="How long to wait for the first discovery snapshot before reconciling anyway",
    )

    def run_once_after_discovery(self) -> None:
        """Wait for the first discovery snapshot (bounded), then reconcile once.

        Intended as a background-thread entry point at app startup. Runs the
        reconcile even when the wait times out: the inventory comes from the
        CLI's own live provider discovery, not from the observe stream.
        """
        deadline = time.monotonic() + self.discovery_wait_timeout_seconds
        while time.monotonic() < deadline:
            if self.backend_resolver.has_completed_initial_discovery():
                break
            threading.Event().wait(timeout=_DISCOVERY_POLL_INTERVAL_SECONDS)
        else:
            logger.debug(
                "Discovery not confirmed within {:.0f}s; reconciling anyway", self.discovery_wait_timeout_seconds
            )
        self.reconcile_now()

    def reconcile_now(self) -> None:
        """Reconcile every Lima / Docker host, then gc stale host records."""
        with log_span("Reconciling Lima/Docker workspace hosts at startup"):
            for provider_name in RECONCILED_PROVIDER_NAMES:
                hosts = self._list_hosts(provider_name)
                for host in hosts:
                    self._reconcile_host(host)
            self._run_gc()

    def _mngr_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["MNGR_HOST_DIR"] = str(self.mngr_host_dir)
        return env

    def _list_hosts(self, provider_name: str) -> list[ListedHost]:
        """List one provider's hosts (agent-less ones included) through the CLI.

        Per-provider (rather than one combined call) so one broken provider --
        e.g. limactl missing on this machine -- cannot hide the other's hosts.
        """
        try:
            return list_provider_hosts(
                self.concurrency_group,
                self.mngr_binary,
                self._mngr_env(),
                provider_name,
                timeout_seconds=_HOST_LIST_TIMEOUT_SECONDS,
            )
        except MngrCommandError as e:
            logger.warning("Could not list {} hosts for the startup reconcile: {}", provider_name, e)
            return []

    def _reconcile_host(self, host: ListedHost) -> None:
        # FAILED / DESTROYED records are dead state, not reconcilable hosts;
        # the gc step owns their eventual deletion.
        if host.state in ("FAILED", "DESTROYED"):
            return

        workspace_id = host.labels.get(WORKSPACE_ID_HOST_LABEL)
        has_system_services = any(agent.name == SYSTEM_SERVICES_AGENT_NAME for agent in host.agents)

        if workspace_id is None:
            # Never touch a host minds didn't stamp. Pre-existing orphans (from
            # before the label existed) stay until cleaned up manually.
            if not host.agents:
                logger.warning(
                    "Found an agent-less {} host '{}' ({}) without a {} label; leaving it alone. "
                    "If it is an orphan, remove it with: mngr destroy @{}.{} --force",
                    host.provider,
                    host.name,
                    host.id,
                    WORKSPACE_ID_HOST_LABEL,
                    host.id,
                    host.provider,
                )
            return

        # A create attempt running in THIS process is not an orphan, whatever its
        # host currently looks like.
        if workspace_id in self.agent_creator.live_in_flight_create_attempt_ids():
            return

        record = self.pending_create_attempt_store.read_record(workspace_id)
        if has_system_services:
            self._adopt_completed_host(host, record)
        else:
            self._maybe_destroy_half_built_host(host, record)

    def _adopt_completed_host(self, host: ListedHost, record: PendingCreateAttemptRecord | None) -> None:
        """Restore a finished-but-unassociated workspace's account association.

        The workspace itself already surfaces through discovery; only the
        account association (recorded by the in-process ``OnCreatedCallback``
        on the happy path) can have been lost. With no pending record left the
        workspace is adopted as-is -- an account-less (private) workspace the
        user can link later.
        """
        services_agent_id = next(agent.id for agent in host.agents if agent.name == SYSTEM_SERVICES_AGENT_NAME)
        if record is None:
            logger.info(
                "Adopting completed {} host '{}' ({}) with no pending-create-attempt record as a private workspace",
                host.provider,
                host.name,
                host.id,
            )
            return

        account_id = record.request.account_id
        if account_id and self.session_store is not None:
            if self.session_store.get_account_for_workspace(services_agent_id) is None:
                try:
                    self.session_store.associate_created_workspace(
                        user_id=account_id,
                        agent_id=services_agent_id,
                        host_id=host.id,
                        display_name=record.request.display_name or record.request.host_name,
                        color=record.request.color,
                        is_cloud_row=False,
                    )
                except WorkspaceSyncError as e:
                    # Keep the record: the next startup's reconcile retries.
                    logger.warning(
                        "Could not restore the account association for adopted workspace {}: {}",
                        services_agent_id,
                        e,
                    )
                    return
                logger.info(
                    "Adopted completed {} host '{}' ({}): re-associated workspace {} with its account",
                    host.provider,
                    host.name,
                    host.id,
                    services_agent_id,
                )
                if isinstance(self.backend_resolver, MngrCliBackendResolver):
                    self.backend_resolver.notify_change()

        # Flip the record to DONE with the canonical ids; the discovery sweep
        # deletes it once the workspace shows up in a snapshot, exactly like a
        # create attempt that finished in-process.
        self.pending_create_attempt_store.mark_done(record.create_attempt_id, services_agent_id, host.id)

    def _maybe_destroy_half_built_host(self, host: ListedHost, record: PendingCreateAttemptRecord | None) -> None:
        """Destroy a labeled half-built host once its record is past the grace window.

        A missing record counts as past grace: the record is written before the
        create spawns, so its absence means it was already handed off (deleted)
        or lost -- either way there is no create left to protect. The record
        (when present) is deliberately kept: it backs the interrupted row's
        retry / dismiss, which work the same whether or not the host still
        exists.
        """
        if record is not None:
            age_seconds = (datetime.now(timezone.utc) - record.created_at).total_seconds()
            if age_seconds < self.half_built_grace_seconds:
                logger.debug(
                    "Leaving half-built {} host '{}' ({}) alone: its create started {:.0f}s ago (grace {:.0f}s)",
                    host.provider,
                    host.name,
                    host.id,
                    age_seconds,
                    self.half_built_grace_seconds,
                )
                return
        argv = [self.mngr_binary, "destroy", f"@{host.id}.{host.provider}", "--force"]
        logger.info(
            "Destroying orphaned half-built {} host '{}' ({}) left by interrupted create attempt {}",
            host.provider,
            host.name,
            host.id,
            host.labels.get(WORKSPACE_ID_HOST_LABEL),
        )
        try:
            run_mngr_to_completion(
                self.concurrency_group, argv, self._mngr_env(), timeout_seconds=_DESTROY_TIMEOUT_SECONDS
            )
        except MngrCommandError as e:
            logger.warning("Could not destroy orphaned host {} ({}): {}", host.name, host.id, e)

    def _run_gc(self) -> None:
        """Sweep stale FAILED / DESTROYED / agent-less host records via ``mngr gc``.

        Scoped to the reconciled providers. gc deletes offline host records
        older than the provider's ``destroyed_host_persisted_seconds`` (the
        7-day default) -- the one retention knob, shared with manual gc runs.
        Its online sweep only destroys agent-less hosts in a terminal state (or
        long quiet ones with recorded activity), so RUNNING half-built hosts
        stay governed by this module's grace-window policy, not gc's.
        """
        argv = [self.mngr_binary, "gc", "--on-error", "continue", "--format", "json"]
        for provider_name in RECONCILED_PROVIDER_NAMES:
            argv.extend(["--provider", provider_name])
        try:
            run_mngr_to_completion(self.concurrency_group, argv, self._mngr_env(), timeout_seconds=_GC_TIMEOUT_SECONDS)
        except MngrCommandError as e:
            logger.warning("Startup gc of lima/docker host records failed: {}", e)
