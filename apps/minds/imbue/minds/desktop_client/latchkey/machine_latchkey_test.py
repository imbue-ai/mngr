from pathlib import Path

import pytest
from pydantic import SecretStr

from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.latchkey.machine_latchkey import machine_latchkey_for_host
from imbue.minds.desktop_client.latchkey.machine_latchkey import machine_latchkey_for_workspace
from imbue.minds.desktop_client.latchkey.machine_latchkey import services_catalog_for_machine
from imbue.minds.desktop_client.latchkey.permission_overview import PermissionOverviewError
from imbue.minds.desktop_client.latchkey.testing import FakeAccountsLatchkey
from imbue.minds.desktop_client.latchkey.testing import build_permissions_test_catalog
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import write_custom_service_registration
from imbue.mngr_latchkey.custom_services import build_custom_service_registration
from imbue.mngr_latchkey.remote._mirror import machine_store_dir
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key
from imbue.mngr_latchkey.services_catalog import ServicesCatalog


def _desktop_latchkey(tmp_path: Path) -> FakeAccountsLatchkey:
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    return FakeAccountsLatchkey(latchkey_directory=latchkey_directory, latchkey_binary="/nonexistent")


def test_a_host_with_no_machine_of_its_own_is_answered_from_this_computer(tmp_path: Path) -> None:
    """A local host's agents run here, so this computer's credentials are theirs."""
    desktop = _desktop_latchkey(tmp_path)

    resolved = machine_latchkey_for_host(desktop, HostId.generate())

    assert resolved.latchkey_directory == desktop.latchkey_directory


def test_a_machine_with_its_own_key_is_answered_from_its_own_store(tmp_path: Path) -> None:
    desktop = _desktop_latchkey(tmp_path)
    host_id = HostId.generate()
    # Recorded when the machine's gateway was first provisioned: the marker that
    # the host keeps credentials of its own.
    store_machine_encryption_key(desktop.plugin_data_dir, host_id, SecretStr("machine-key-2748"))

    resolved = machine_latchkey_for_host(desktop, host_id)

    assert resolved.latchkey_directory == machine_store_dir(desktop.plugin_data_dir, host_id)


def test_resolving_a_machine_materializes_its_store(tmp_path: Path) -> None:
    """The store has to be usable as a LATCHKEY_DIRECTORY before anything reads it."""
    desktop = _desktop_latchkey(tmp_path)
    host_id = HostId.generate()
    store_machine_encryption_key(desktop.plugin_data_dir, host_id, SecretStr("machine-key-9145"))

    resolved = machine_latchkey_for_host(desktop, host_id)

    assert (resolved.latchkey_directory / "encryption_key").is_symlink()


def test_a_workspace_whose_host_is_unknown_is_refused_rather_than_answered_from_here(tmp_path: Path) -> None:
    """Answering from this computer would show, and connect, the wrong machine's accounts."""
    desktop = _desktop_latchkey(tmp_path)
    resolver = StaticBackendResolver(url_by_agent_and_service={})

    with pytest.raises(PermissionOverviewError):
        machine_latchkey_for_workspace(desktop, resolver, str(AgentId()))


def test_a_machines_catalog_names_only_the_custom_services_of_its_own_store(tmp_path: Path) -> None:
    """The shipped half of the catalog is kept; the overlay is the machine store's registrations, not the union."""
    desktop = _desktop_latchkey(tmp_path)
    write_custom_service_registration(
        desktop.latchkey_directory, "custom_https_local_test", build_custom_service_registration("local.test", "https")
    )
    host_id = HostId.generate()
    store_machine_encryption_key(desktop.plugin_data_dir, host_id, SecretStr("machine-key-6a31"))
    machine = machine_latchkey_for_host(desktop, host_id)
    write_custom_service_registration(
        machine.latchkey_directory,
        "custom_https_remote_test",
        build_custom_service_registration("remote.test", "https"),
    )
    union_catalog = ServicesCatalog(latchkey_directory=desktop.latchkey_directory, is_including_every_machine=True)

    machine_catalog = services_catalog_for_machine(union_catalog, machine)
    from_override = services_catalog_for_machine(build_permissions_test_catalog(), machine)

    assert {"custom_https_local_test", "custom_https_remote_test"} <= union_catalog.all_service_names()
    assert machine_catalog.latchkey_directory == machine.latchkey_directory
    assert machine_catalog.is_including_every_machine is False
    assert "custom_https_remote_test" in machine_catalog.all_service_names()
    assert "custom_https_local_test" not in machine_catalog.all_service_names()
    assert build_permissions_test_catalog().all_service_names() < from_override.all_service_names()
    assert "custom_https_remote_test" in from_override.all_service_names()
