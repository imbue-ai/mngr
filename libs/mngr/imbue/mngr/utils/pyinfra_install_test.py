from pathlib import Path

import pyinfra

from imbue.mngr.utils.pyinfra_install import take_pyinfra_install_snapshot


def test_snapshot_of_a_healthy_install_finds_the_ssh_connector_beside_the_imported_package() -> None:
    snapshot = take_pyinfra_install_snapshot()

    assert snapshot.module_dir == Path(pyinfra.__file__).parent
    assert snapshot.is_module_dir_present
    assert snapshot.is_distribution_beside_module
    assert snapshot.distribution_version is not None
    assert snapshot.connector_names is not None and "ssh" in snapshot.connector_names
    assert snapshot.execution_connector_names is not None and "ssh" in snapshot.execution_connector_names
    assert snapshot.connector_load_error is None
    assert snapshot.is_sys_prefix_present
    assert snapshot.site_packages_modified_at is not None
    assert snapshot.process_uptime_seconds >= 0
