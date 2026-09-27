"""Reaching a remote workspace's own machine from the desktop app.

A remote workspace's credentials and the policy its gateway enforces live on
its own machine (its VPS). Everything the app does with either -- showing the
Permissions tab, connecting a service, signing one out, flipping a toggle --
therefore has to go *to* that machine, and this module is the door: it opens
the workspace's outer host and hands back a
:class:`~imbue.mngr_latchkey.remote.credentials.MachineCredentials` bound to it.

Opening that door needs mngr's provider set, which is loaded from the same
settings the ``mngr`` CLI reads. It is loaded **lazily and kept across calls**,
because loading it imports every installed provider plugin, which is seconds of
work that must not land on the first click. :meth:`warm` starts that load off
the request path at startup, so in practice the first Permissions tab open finds
it done. What is kept is dropped and reloaded whenever those settings change on
disk, because this app writes them itself -- signing an account in registers a
new provider instance, and a workspace created on it would otherwise be
unreachable until the app restarted.

Every call here is synchronous and blocks its caller until the machine has
answered. That is the point: a workspace's Permissions tab shows what its
machine holds, not what this computer last heard, and a change is reported as
made only once the machine has taken it. A local workspace has no machine of its
own -- its agents run here, on the credentials and the permissions file this
computer keeps -- and :meth:`MachineAccess.machine_host_for` is what says so, so
its caller can leave the local edit as the whole change.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.latchkey.permission_overview import resolve_workspace_host_id
from imbue.mngr.api.providers import close_provider_instances_for_context
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.plugin_manager import get_or_create_plugin_manager
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.host_dir import read_default_host_dir
from imbue.mngr.config.loader import get_or_create_profile_dir
from imbue.mngr.config.loader import load_config
from imbue.mngr.config.pre_readers import get_user_config_path
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.remote.credentials import MachineCredentials
from imbue.mngr_latchkey.remote.credentials import has_machine_of_its_own
from imbue.mngr_latchkey.store import LatchkeyStoreError

# Name of the thread the provider set is pre-loaded on.
_WARM_THREAD_NAME: Final[str] = "latchkey-machine-access-warm"


class _ProviderSettingsStamp(FrozenModel):
    """What the settings file a provider set was loaded from looked like at the time."""

    inode: int = Field(description="Identifies the file itself, which an atomic rewrite replaces.")
    modified_time_in_nanoseconds: int = Field(description="When the file was last written.")
    size_in_bytes: int = Field(description="How long the file was.")


class MachineUnreachableError(Exception):
    """Raised when a workspace's machine could not be reached, or refused what it was asked.

    Always something the user is waiting on the answer to, so it carries the
    machine's own reason and is shown where they clicked.
    """


class MachineAccess(MutableModel):
    """Opens the machine behind a remote workspace, so its own state can be read and edited.

    Holds the mngr provider set the door needs (see the module docstring) and
    resolves, per workspace, whether there is a machine to open at all.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    latchkey: Latchkey = Field(frozen=True, description="This computer's latchkey, which owns every machine store.")
    concurrency_group: ConcurrencyGroup = Field(
        frozen=True,
        description=(
            "Owns whatever long-lived resources the provider plugins register (an ``mngr`` command hands "
            "its own here); the app's root group, so they live exactly as long as the app does."
        ),
    )
    backend_resolver: BackendResolverInterface = Field(
        frozen=True,
        description="Discovery state that maps a workspace to its host and provider.",
    )

    _mngr_ctx: MngrContext | None = PrivateAttr(default=None)
    _mngr_ctx_settings_stamp: _ProviderSettingsStamp | None = PrivateAttr(default=None)
    _mngr_ctx_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def warm(self) -> None:
        """Start loading the provider set off the request path.

        Best-effort and unchecked: a failure here only means the first machine
        operation pays the load itself (and reports its own failure), so it must
        never tear the app down.
        """
        self.concurrency_group.start_new_thread(
            target=self._warm_provider_set,
            name=_WARM_THREAD_NAME,
            daemon=True,
            is_checked=False,
        )

    def machine_host_for(self, workspace_agent_id: str) -> HostId | None:
        """Return the host of ``workspace_agent_id`` when that host is a machine of its own.

        ``None`` for a workspace whose credentials and policy are this
        computer's -- a local one, or a remote one whose gateway has not been
        provisioned from here yet -- which is exactly when there is nothing to
        carry anywhere.

        Raises:
            MachineUnreachableError: when the machine store cannot be read, so
                it is unknown whether there is anything to carry.
        """
        host_id = resolve_workspace_host_id(self.backend_resolver, workspace_agent_id)
        if host_id is None:
            return None
        try:
            return host_id if has_machine_of_its_own(self.latchkey.plugin_data_dir, host_id) else None
        except LatchkeyStoreError as e:
            raise MachineUnreachableError(f"Could not open the machine store of host {host_id}: {e}") from e

    @contextmanager
    def open_machine(self, workspace_agent_id: str, host_id: HostId) -> Iterator[MachineCredentials]:
        """Open ``host_id``'s machine for the duration of one exchange.

        Raises:
            MachineUnreachableError: when the provider set cannot be loaded, the
                workspace's provider is unknown, or the provider has no remote
                machine for it. Whatever opening the machine or the exchange
                itself raises is left to the caller, whose
                :class:`~imbue.minds.desktop_client.latchkey.machine_operations.MachineOperator`
                describes it in terms of what the user was trying to do.
        """
        provider_name = self._provider_name_for(workspace_agent_id)
        try:
            provider = self._provider_for(provider_name)
        except (MngrError, OSError) as e:
            raise MachineUnreachableError(f"Could not reach the machine of host {host_id}: {e}") from e
        with self._opened_outer_host(provider, provider_name, host_id) as outer:
            if outer is None or outer.is_local:
                raise MachineUnreachableError(
                    f"Workspace {workspace_agent_id} is recorded as having a machine of its own, but provider "
                    f"{provider_name} offers no remote machine for host {host_id}."
                )
            yield MachineCredentials(host=outer, latchkey=self.latchkey, host_id=host_id)

    @contextmanager
    def _opened_outer_host(
        self, provider: ProviderInstanceInterface, provider_name: str, host_id: HostId
    ) -> Iterator[OuterHostInterface | None]:
        """Open a host's outer machine, looking again on fresh data if the provider has not heard of it.

        A provider instance is kept across calls, and some providers cache their
        whole host/lease listing on it with no expiry (imbue_cloud does). A
        workspace leased *after* that listing was taken would then be invisible
        for as long as that instance is kept -- its Permissions tab unreachable,
        and leasing one is not a settings change, so nothing reloads it away --
        so "not found" is treated as "our listing may be older than this
        workspace" and asked once more.
        """
        is_machine_handed_over = False
        try:
            with provider.outer_host_for(host_id) as outer:
                # Only the lookup gets a second chance. Past this point the
                # caller's exchange owns the failure, and re-opening under it
                # would hand the same caller a second machine.
                is_machine_handed_over = True
                yield outer
                return
        except HostNotFoundError:
            if is_machine_handed_over:
                raise
            logger.debug(
                "Host {} not in provider {}'s cached listing; refreshing it and looking again", host_id, provider_name
            )
        provider.reset_caches()
        with provider.outer_host_for(host_id) as outer:
            yield outer

    def _provider_for(self, provider_name: str) -> ProviderInstanceInterface:
        """Return the provider instance a machine is opened through (a seam for tests)."""
        return get_provider_instance(ProviderInstanceName(provider_name), self._provider_context())

    def _provider_name_for(self, workspace_agent_id: str) -> str:
        """The provider instance the workspace's host runs on, as discovery reported it."""
        try:
            parsed = AgentId(workspace_agent_id)
        except ValueError as e:
            raise MachineUnreachableError(f"'{workspace_agent_id}' is not a workspace this app knows.") from e
        info = self.backend_resolver.get_agent_display_info(parsed)
        if info is None or not info.provider_name:
            raise MachineUnreachableError(
                f"Minds does not know which provider workspace {workspace_agent_id} runs on yet, so it cannot "
                "reach its machine. Try again in a moment."
            )
        return info.provider_name

    def _provider_context(self) -> MngrContext:
        """The loaded mngr context, reloaded whenever the settings behind it have changed.

        One context is kept across calls, because the provider instances cached
        against it hold the connections and listings that make a second machine
        operation cheaper than the first. Keeping it *unconditionally* is what
        must not happen: this app registers a provider instance per signed-in
        account in the very settings file the context was loaded from, so a
        context taken before an account signed in knows nothing of the provider
        its workspaces run on, and every machine operation on one of them fails
        for as long as the app stays up.

        Watching the user settings file rather than being told when it changes
        covers every writer of it, including a user editing it by hand while the
        app runs. That is the layer this app writes; a change to the project,
        local or environment layers ``load_config`` also merges is picked up by
        the next reload or by a restart, not by itself.

        Raises:
            MachineUnreachableError: when the settings cannot be loaded and none
                have been loaded yet.
        """
        with self._mngr_ctx_lock:
            kept_ctx = self._mngr_ctx
            # Stamped before the load, not after: the load is seconds long, and
            # a write that lands inside it would otherwise be stamped as one
            # this context already carries -- leaving it stale for good.
            settings_stamp = _read_settings_stamp()
            if kept_ctx is not None and settings_stamp == self._mngr_ctx_settings_stamp:
                return kept_ctx
            try:
                # Loaded under the lock: it is seconds of plugin imports, and a
                # second caller arriving mid-load wants that one, not its own.
                reloaded_ctx = self._load_context()
            except MachineUnreachableError as e:
                if kept_ctx is None:
                    raise
                # The settings changed into something that will not load -- a
                # hand edit with a typo, a block naming a backend this install
                # has not got. The set in hand still opens every machine it was
                # loaded for, and the stamp is left as it was, so the next
                # operation reads the new settings again.
                logger.warning("Could not reload the mngr provider set; keeping the one already loaded: {}", e)
                return kept_ctx
            self._mngr_ctx = reloaded_ctx
            self._mngr_ctx_settings_stamp = settings_stamp
        # Closing can block, so it happens off the lock.
        if kept_ctx is not None:
            logger.debug("mngr settings changed; retiring the provider set loaded from them")
            close_provider_instances_for_context(kept_ctx)
            # Safe only because the reload built a whole new context, which
            # brought a watchdog of its own: the retired one would otherwise
            # keep its thread for the life of the app, one more per reload.
            kept_ctx.suspension_watchdog.shutdown()
        return reloaded_ctx

    def _load_context(self) -> MngrContext:
        """Load a fresh mngr context (a seam for tests).

        Raises:
            MachineUnreachableError: when the settings cannot be loaded.
        """
        return _load_provider_context(self.concurrency_group)

    def _warm_provider_set(self) -> None:
        try:
            self._provider_context()
        except MachineUnreachableError as e:
            logger.warning("Could not pre-load the provider set for latchkey machine access: {}", e)


def _read_settings_stamp() -> _ProviderSettingsStamp | None:
    """Stamp the user settings file a context would be loaded from, or None when it cannot be read.

    Resolved the way ``load_config`` resolves it, rather than from a loaded
    context, so a stamp can be taken *before* a load as well as after one.

    None is a stamp like any other: it says the file was not there, so a file
    appearing (mngr initialising after this process started) reads as a change,
    and a file that is never there never does.
    """
    try:
        settings_path = get_user_config_path(get_or_create_profile_dir(read_default_host_dir()))
        stat_result = settings_path.stat()
    except (MngrError, OSError) as e:
        logger.debug("Could not stat the mngr settings file: {}", e)
        return None
    return _ProviderSettingsStamp(
        inode=stat_result.st_ino,
        modified_time_in_nanoseconds=stat_result.st_mtime_ns,
        size_in_bytes=stat_result.st_size,
    )


def _load_provider_context(concurrency_group: ConcurrencyGroup) -> MngrContext:
    """Load mngr's settings the way the CLI does, so this process can open a workspace's machine.

    Read non-strictly, because the settings are the whole CLI's while the
    plugins are only the ones this app depends on: the layers include the
    project settings of whatever checkout the app was launched from, which may
    configure a backend no minds build ships. Strictly, one such block fails
    the load, and every workspace loses the machine behind it over a provider
    this app would never open. Non-strict skips the block with a warning, and
    drops any settings field this build does not know; a malformed value in a
    block minds does use still fails the load.

    Raises:
        MachineUnreachableError: when the settings cannot be loaded.
    """
    try:
        return load_config(get_or_create_plugin_manager(), concurrency_group, strict=False)
    except (MngrError, OSError) as e:
        raise MachineUnreachableError(
            f"Could not load the mngr settings that name your workspaces' machines: {e}"
        ) from e
