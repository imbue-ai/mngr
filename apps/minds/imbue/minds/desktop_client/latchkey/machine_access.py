"""Reaching a remote workspace's own machine from the desktop app.

A remote workspace's credentials and the policy its gateway enforces live on
its own machine (its VPS). Everything the app does with either -- showing the
Permissions tab, connecting a service, signing one out, flipping a toggle --
therefore has to go *to* that machine, and this module is the door: it opens
the workspace's outer host and hands back a
:class:`~imbue.mngr_latchkey.remote.credentials.MachineCredentials` bound to it.

Opening that door needs mngr's provider set, which is loaded from the same
settings the ``mngr`` CLI reads. It is loaded **once, lazily**, and kept for the
life of the process: loading it imports every installed provider plugin, which
is seconds of work that must not land on the first click. :meth:`warm` starts
that load off the request path at startup, so in practice the first Permissions
tab open finds it done.

Every call here is synchronous and blocks its caller until the machine has
answered. That is the point: a workspace's Permissions tab shows what its
machine holds, not what this computer last heard, and a change is reported as
made only once the machine has taken it. A local workspace has no machine of its
own -- its agents run here, on the credentials and the permissions file this
computer keeps -- and :meth:`MachineAccess.machine_host_for` is what says so, so
its caller can leave the local edit as the whole change.
"""

import threading
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.latchkey.permission_overview import resolve_workspace_host_id
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.plugin_manager import get_or_create_plugin_manager
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.loader import load_config
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
    get_backend_resolver: Callable[[], BackendResolverInterface] = Field(
        frozen=True,
        description=(
            "Looks up the resolver that maps a workspace to its host and provider. Resolved per call "
            "rather than held, because this is built at startup, before the app state it lives in exists."
        ),
    )

    _mngr_ctx: MngrContext | None = PrivateAttr(default=None)
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
        host_id = resolve_workspace_host_id(self.get_backend_resolver(), workspace_agent_id)
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

        This process holds one long-lived provider instance, and some providers
        cache their whole host/lease listing on it with no expiry (imbue_cloud
        does). A workspace leased *after* that listing was taken would then be
        permanently invisible -- its Permissions tab unreachable until the app
        restarts -- so "not found" is treated as "our listing may be older than
        this workspace" and asked once more.
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
        info = self.get_backend_resolver().get_agent_display_info(parsed)
        if info is None or not info.provider_name:
            raise MachineUnreachableError(
                f"Minds does not know which provider workspace {workspace_agent_id} runs on yet, so it cannot "
                "reach its machine. Try again in a moment."
            )
        return info.provider_name

    def _provider_context(self) -> MngrContext:
        """The loaded mngr context, loading it on first use.

        One context for the life of the process, because the provider instances
        cached against it hold the connections and listings that make a second
        machine operation cheaper than the first.

        Raises:
            MachineUnreachableError: when the settings cannot be loaded.
        """
        with self._mngr_ctx_lock:
            if self._mngr_ctx is None:
                self._mngr_ctx = _load_provider_context(self.concurrency_group)
            return self._mngr_ctx

    def _warm_provider_set(self) -> None:
        try:
            self._provider_context()
        except MachineUnreachableError as e:
            logger.warning("Could not pre-load the provider set for latchkey machine access: {}", e)


def _load_provider_context(concurrency_group: ConcurrencyGroup) -> MngrContext:
    """Load mngr's settings the way the CLI does, so this process can open a workspace's machine.

    Raises:
        MachineUnreachableError: when the settings cannot be loaded.
    """
    try:
        return load_config(get_or_create_plugin_manager(), concurrency_group)
    except (MngrError, OSError) as e:
        raise MachineUnreachableError(
            f"Could not load the mngr settings that name your workspaces' machines: {e}"
        ) from e
