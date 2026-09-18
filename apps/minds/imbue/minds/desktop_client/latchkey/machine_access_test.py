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
from imbue.minds.desktop_client.latchkey.machine_access import _load_provider_context
from imbue.minds.desktop_client.latchkey.testing import FakeAccountsLatchkey
from imbue.minds.desktop_client.latchkey.testing import FixedHostBackendResolver
from imbue.mngr.api.providers import reset_provider_instances
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.pre_readers import get_user_config_path
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import UnknownBackendError
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key


class _StubProvider(MutableModel):
    """A provider whose host listing only knows the host after its caches are reset.

    Which is the shape of the problem: a provider instance is kept across calls,
    and a workspace leased after its listing was taken is invisible to it until
    something refreshes that listing -- leasing one is not a settings change, so
    nothing reloads the instance away either.
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


def test_a_provider_block_for_a_backend_this_app_lacks_does_not_cost_any_workspace_its_machine(
    project_config_dir: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    cg: ConcurrencyGroup,
    log_warnings: list[str],
) -> None:
    """The settings read here are the whole CLI's, and may name a backend this app has no plugin for.

    Refusing such a block would take out the machine behind every workspace,
    over a provider this app would never open.
    """
    (project_config_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n"
        '[providers.no_such_cloud]\nbackend = "no_such_cloud"\n\n'
        '[providers.here]\nbackend = "local"\n'
    )
    monkeypatch.chdir(temp_git_repo)

    mngr_ctx = _load_provider_context(cg)

    assert ProviderInstanceName("here") in mngr_ctx.config.providers
    assert ProviderInstanceName("no_such_cloud") not in mngr_ctx.config.providers
    assert any("no_such_cloud" in message for message in log_warnings), log_warnings


def _settings_registering(provider_name: str) -> str:
    """A settings file that registers ``provider_name``, opted into being loaded under pytest."""
    return f'is_allowed_in_pytest = true\n\n[providers."{provider_name}"]\nbackend = "local"\n'


@contextmanager
def _settings_only_access(
    tmp_path: Path, access_class: type[MachineAccess] = MachineAccess
) -> Iterator[MachineAccess]:
    """A ``MachineAccess`` that loads its provider set from the real mngr settings."""
    latchkey_directory = tmp_path / "settings-latchkey"
    latchkey_directory.mkdir()
    try:
        with ConcurrencyGroup(name="machine-access-settings-test") as concurrency_group:
            yield access_class(
                latchkey=FakeAccountsLatchkey(latchkey_directory=latchkey_directory, latchkey_binary="/nonexistent"),
                concurrency_group=concurrency_group,
                backend_resolver=FixedHostBackendResolver(
                    url_by_agent_and_service={}, fixed_host_id=HostId.generate(), known_agent_ids=()
                ),
            )
    finally:
        # These load real contexts, so the instances they build are cached
        # globally against them; dropped here so none outlives the concurrency
        # group it was built in, as ``temp_mngr_ctx`` does for the tests that
        # take their context from the fixture.
        reset_provider_instances()


def test_a_provider_registered_after_the_provider_set_was_loaded_is_reachable(tmp_path: Path) -> None:
    """Signing an account in registers its provider, and its workspaces must be reachable at once.

    A provider set kept from before the sign-in knows that provider only as a
    name, which resolves as a backend that does not exist -- so every machine
    operation on that account's workspaces fails until the app is restarted.
    """
    provider_name = "imbue_cloud_someone-example-com"
    with _settings_only_access(tmp_path) as access:
        settings_path = get_user_config_path(access._provider_context().profile_dir)
        with pytest.raises(UnknownBackendError):
            access._provider_for(provider_name)

        settings_path.write_text(_settings_registering(provider_name))

        assert str(access._provider_for(provider_name).name) == provider_name


def test_a_settings_change_made_during_the_load_is_not_taken_for_one_already_loaded(tmp_path: Path) -> None:
    """Loading is seconds of plugin imports, and a sign-in can land inside that window.

    A stamp taken after the load records such a write as one the context it
    returns already carries, and the settings then stay stale for the life of
    the app -- which is the whole failure the stamping exists to prevent.
    """
    provider_name = "imbue_cloud_signed-in-mid-load"

    class _AccessWrittenToMidLoad(MachineAccess):
        """Access whose load has the settings file written under it, as a sign-in would."""

        def _load_context(self) -> MngrContext:
            loaded = super()._load_context()
            settings_path = get_user_config_path(loaded.profile_dir)
            if not settings_path.exists():
                settings_path.write_text(_settings_registering(provider_name))
            return loaded

    with _settings_only_access(tmp_path, access_class=_AccessWrittenToMidLoad) as access:
        access._provider_context()

        assert str(access._provider_for(provider_name).name) == provider_name


def test_the_loaded_provider_set_is_kept_while_the_settings_are_unchanged(tmp_path: Path) -> None:
    """Reloading costs seconds of plugin imports and drops every cached connection."""
    with _settings_only_access(tmp_path) as access:
        settings_path = get_user_config_path(access._provider_context().profile_dir)
        settings_path.write_text(_settings_registering("imbue_cloud_unchanged-account"))

        assert access._provider_context() is access._provider_context()


def test_a_retired_context_does_not_leave_its_suspension_watchdog_running(tmp_path: Path) -> None:
    """Its thread runs until shutdown or process exit, so a reload would leak one every time."""
    with _settings_only_access(tmp_path) as access:
        retired_ctx = access._provider_context()
        get_user_config_path(retired_ctx.profile_dir).write_text(_settings_registering("imbue_cloud_retired-account"))

        assert access._provider_context() is not retired_ctx
        assert retired_ctx.suspension_watchdog._shutdown_event.is_set()


def test_settings_that_will_not_load_keep_the_provider_set_already_loaded(tmp_path: Path) -> None:
    """A hand edit that does not parse must not take down every machine operation.

    The set already in hand still opens every machine it was loaded for, so it
    is kept rather than dropped, and the settings are read again on the next
    operation -- which is what makes the app recover once they parse.
    """
    with _settings_only_access(tmp_path) as access:
        loaded_ctx = access._provider_context()
        get_user_config_path(loaded_ctx.profile_dir).write_text("is_allowed_in_pytest = true\n[providers.\n")

        assert access._provider_context() is loaded_ctx
        assert not loaded_ctx.suspension_watchdog._shutdown_event.is_set()
