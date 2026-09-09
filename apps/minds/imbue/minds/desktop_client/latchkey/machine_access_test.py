from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest
from pydantic import Field
from pydantic import SecretStr
from pydantic import SkipValidation

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.latchkey.machine_access import MachineAccess
from imbue.minds.desktop_client.latchkey.machine_access import MachineUnreachableError
from imbue.minds.desktop_client.latchkey.testing import FakeAccountsLatchkey
from imbue.minds.desktop_client.latchkey.testing import FixedHostBackendResolver
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key


class _StubProvider(MutableModel):
    """A provider whose host listing only knows the host after its caches are reset.

    Which is the shape of the problem: this process holds one long-lived
    provider instance, and a workspace leased after its listing was taken is
    invisible to it until something refreshes that listing.
    """

    model_config = {"arbitrary_types_allowed": True}

    outer: SkipValidation[OuterHostInterface] = Field(
        description="What the host resolves to once the listing knows it."
    )
    is_listing_fresh: bool = Field(default=False, description="Whether the listing has been refreshed yet.")
    reset_count: int = Field(default=0, description="How many times the listing was refreshed.")

    def reset_caches(self) -> None:
        self.reset_count += 1
        self.is_listing_fresh = True

    @contextmanager
    def outer_host_for(self, host_id: HostId) -> Iterator[OuterHostInterface | None]:
        if not self.is_listing_fresh:
            raise HostNotFoundError(ProviderInstanceName("imbue_cloud"), host_id)
        yield self.outer


class _RemoteOuter(MutableModel):
    """The bare surface :meth:`MachineAccess.open_machine` reads off an outer host."""

    is_local: bool = Field(default=False, description="Whether this outer host is the local machine.")

    def get_name(self) -> str:
        return "vps-test"


def _access(tmp_path: Path, provider: _StubProvider) -> tuple[MachineAccess, HostId, str]:
    """A ``MachineAccess`` over one remote workspace, pinned to ``provider``."""
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    latchkey = FakeAccountsLatchkey(latchkey_directory=latchkey_directory, latchkey_binary="/nonexistent")
    agent_id = AgentId()
    host_id = HostId.generate()
    store_machine_encryption_key(latchkey.plugin_data_dir, host_id, SecretStr("machine-key-4471"))

    class _PinnedAccess(MachineAccess):
        """Access whose provider lookup is the stub, so only the listing behaviour is under test."""

        def _provider_name_for(self, workspace_agent_id: str) -> str:
            del workspace_agent_id
            return "imbue_cloud"

        def _provider_for(self, provider_name: str) -> ProviderInstanceInterface:
            del provider_name
            return cast(ProviderInstanceInterface, provider)

    access = _PinnedAccess(
        latchkey=latchkey,
        concurrency_group=ConcurrencyGroup(name="machine-access-test"),
        backend_resolver=FixedHostBackendResolver(
            url_by_agent_and_service={}, fixed_host_id=host_id, known_agent_ids=(agent_id,)
        ),
    )
    return access, host_id, str(agent_id)


def test_a_workspace_leased_after_the_listing_was_taken_is_still_reachable(tmp_path: Path) -> None:
    """A freshly created workspace must not be invisible until the app restarts."""
    provider = _StubProvider(outer=cast(OuterHostInterface, _RemoteOuter()))
    access, host_id, agent_id = _access(tmp_path, provider)

    with access.open_machine(agent_id, host_id) as machine:
        assert machine.host_id == host_id

    assert provider.reset_count == 1


def test_a_workspace_whose_provider_offers_no_remote_machine_is_refused(tmp_path: Path) -> None:
    """A local outer host is not a machine of its own; guessing would edit this computer's own state."""
    provider = _StubProvider(outer=cast(OuterHostInterface, _RemoteOuter(is_local=True)), is_listing_fresh=True)
    access, host_id, agent_id = _access(tmp_path, provider)

    with pytest.raises(MachineUnreachableError, match="no remote machine"):
        with access.open_machine(agent_id, host_id):
            pytest.fail("a local outer host must not be handed out as a machine")


def test_a_local_workspace_has_no_machine_to_open(tmp_path: Path) -> None:
    """No recorded encryption key means no provisioning pass from here has reached a machine."""
    provider = _StubProvider(outer=cast(OuterHostInterface, _RemoteOuter()), is_listing_fresh=True)
    access, _host_id, agent_id = _access(tmp_path, provider)
    (access.latchkey.plugin_data_dir / "hosts").rename(access.latchkey.plugin_data_dir / "hosts-moved-aside")

    assert access.machine_host_for(agent_id) is None
