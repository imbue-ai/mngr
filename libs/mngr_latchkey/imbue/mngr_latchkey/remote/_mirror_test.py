import os
import stat
from pathlib import Path

import pytest

from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import BROWSER_STATE_FILENAME
from imbue.mngr_latchkey.core import CONFIG_FILENAME
from imbue.mngr_latchkey.core import CREDENTIALS_STORE_FILENAME
from imbue.mngr_latchkey.core import DAILY_COUNT_STAMP_FILENAME
from imbue.mngr_latchkey.core import PERMISSIONS_CONFIG_FILENAME
from imbue.mngr_latchkey.core import UPSTREAM_DATA_FORMAT_VERSION_FILENAME
from imbue.mngr_latchkey.encryption_key import ENCRYPTION_KEY_FILENAME
from imbue.mngr_latchkey.encryption_key import encryption_key_path
from imbue.mngr_latchkey.encryption_key import load_or_create_encryption_key
from imbue.mngr_latchkey.remote._mirror import clear_machine_credentials
from imbue.mngr_latchkey.remote._mirror import machine_credentials_path
from imbue.mngr_latchkey.remote._mirror import machine_store_dir
from imbue.mngr_latchkey.remote._mirror import materialize_machine_store
from imbue.mngr_latchkey.remote._mirror import write_machine_credentials
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    """Return the desktop latchkey directory and its plugin data dir."""
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    return latchkey_directory, plugin_data_dir(latchkey_directory)


def test_machine_store_is_the_directory_holding_the_hosts_permissions_file(tmp_path: Path) -> None:
    """The machine store and the canonical permissions file must not be able to drift apart."""
    _, data_dir = _roots(tmp_path)
    host_id = HostId.generate()

    assert machine_store_dir(data_dir, host_id) == permissions_path_for_host(data_dir, host_id).parent


def test_materialize_shares_config_browser_state_key_and_count_stamp_by_relative_symlink(tmp_path: Path) -> None:
    latchkey_directory, data_dir = _roots(tmp_path)
    host_id = HostId.generate()

    store_dir = materialize_machine_store(latchkey_directory, data_dir, host_id)

    for filename in (CONFIG_FILENAME, BROWSER_STATE_FILENAME, ENCRYPTION_KEY_FILENAME, DAILY_COUNT_STAMP_FILENAME):
        link = store_dir / filename
        assert link.is_symlink()
        # Relative, so the tree survives being moved or copied wholesale.
        assert not os.path.isabs(os.readlink(link))
        assert link.resolve() == (latchkey_directory / filename).resolve()


def test_materialize_shares_the_daily_count_stamp_so_the_ping_is_per_user(tmp_path: Path) -> None:
    """A per-store stamp would fire latchkey's once-a-day usage ping once per remote host."""
    latchkey_directory, data_dir = _roots(tmp_path)
    store_dir = materialize_machine_store(latchkey_directory, data_dir, HostId.generate())

    # The desktop has never pinged, so the link dangles until upstream stamps
    # the first one -- through the link, as writing here does.
    (store_dir / DAILY_COUNT_STAMP_FILENAME).write_text("2024-05-06T07:08:09.000Z")

    assert (latchkey_directory / DAILY_COUNT_STAMP_FILENAME).read_text() == "2024-05-06T07:08:09.000Z"


def test_materialize_creates_the_desktop_key_so_the_link_never_dangles(tmp_path: Path) -> None:
    """A dangling key link would make the CLI mint a second key inside the machine store."""
    latchkey_directory, data_dir = _roots(tmp_path)
    host_id = HostId.generate()

    store_dir = materialize_machine_store(latchkey_directory, data_dir, host_id)

    assert encryption_key_path(latchkey_directory).is_file()
    assert (store_dir / ENCRYPTION_KEY_FILENAME).read_text() == encryption_key_path(latchkey_directory).read_text()


def test_materialize_links_upstream_permissions_name_at_the_canonical_file(tmp_path: Path) -> None:
    """A bare ``latchkey`` run against a machine store must enforce what the host's agents are held to."""
    latchkey_directory, data_dir = _roots(tmp_path)
    host_id = HostId.generate()

    store_dir = materialize_machine_store(latchkey_directory, data_dir, host_id)
    permissions_path_for_host(data_dir, host_id).write_text('{"rules": []}')

    assert (store_dir / PERMISSIONS_CONFIG_FILENAME).read_text() == '{"rules": []}'


def test_materialize_leaves_the_store_owner_only(tmp_path: Path) -> None:
    latchkey_directory, data_dir = _roots(tmp_path)

    store_dir = materialize_machine_store(latchkey_directory, data_dir, HostId.generate())

    assert stat.S_IMODE(store_dir.stat().st_mode) == 0o700


def test_materialize_is_idempotent_and_keeps_the_mirror(tmp_path: Path) -> None:
    latchkey_directory, data_dir = _roots(tmp_path)
    host_id = HostId.generate()
    store_dir = materialize_machine_store(latchkey_directory, data_dir, host_id)
    write_machine_credentials(store_dir, b"encrypted-4471", "2")

    materialize_machine_store(latchkey_directory, data_dir, host_id)

    assert machine_credentials_path(data_dir, host_id).read_bytes() == b"encrypted-4471"


def test_materialize_repoints_a_link_that_points_elsewhere(tmp_path: Path) -> None:
    latchkey_directory, data_dir = _roots(tmp_path)
    host_id = HostId.generate()
    store_dir = machine_store_dir(data_dir, host_id)
    store_dir.mkdir(parents=True)
    stray = tmp_path / "somewhere-else.json"
    stray.write_text("{}")
    (store_dir / CONFIG_FILENAME).symlink_to(stray)

    materialize_machine_store(latchkey_directory, data_dir, host_id)

    assert (store_dir / CONFIG_FILENAME).resolve() == (latchkey_directory / CONFIG_FILENAME).resolve()


def test_materialize_refuses_to_replace_a_regular_file_with_a_shared_link(tmp_path: Path) -> None:
    """A real file where a shared link belongs holds state; deleting it silently would lose it."""
    latchkey_directory, data_dir = _roots(tmp_path)
    host_id = HostId.generate()
    store_dir = machine_store_dir(data_dir, host_id)
    store_dir.mkdir(parents=True)
    (store_dir / BROWSER_STATE_FILENAME).write_text("an independent browser session")

    with pytest.raises(LatchkeyStoreError, match=BROWSER_STATE_FILENAME):
        materialize_machine_store(latchkey_directory, data_dir, host_id)

    assert (store_dir / BROWSER_STATE_FILENAME).read_text() == "an independent browser session"


def test_write_machine_credentials_stamps_the_format_version_alongside_the_mirror(tmp_path: Path) -> None:
    latchkey_directory, data_dir = _roots(tmp_path)
    host_id = HostId.generate()
    store_dir = materialize_machine_store(latchkey_directory, data_dir, host_id)

    write_machine_credentials(store_dir, b"encrypted-8823", "3")

    assert (store_dir / CREDENTIALS_STORE_FILENAME).read_bytes() == b"encrypted-8823"
    # Without its own stamp the upstream CLI would "migrate" an already-current
    # mirror the first time it read it.
    assert (store_dir / UPSTREAM_DATA_FORMAT_VERSION_FILENAME).read_text() == "3"


def test_write_machine_credentials_replaces_a_previous_mirror(tmp_path: Path) -> None:
    latchkey_directory, data_dir = _roots(tmp_path)
    host_id = HostId.generate()
    store_dir = materialize_machine_store(latchkey_directory, data_dir, host_id)
    write_machine_credentials(store_dir, b"encrypted-8823", "3")

    write_machine_credentials(store_dir, b"encrypted-6612", "3")

    assert (store_dir / CREDENTIALS_STORE_FILENAME).read_bytes() == b"encrypted-6612"


def test_write_machine_credentials_writes_through_the_shared_links_untouched(tmp_path: Path) -> None:
    """Writing a mirror must never turn a shared link into an independent copy."""
    latchkey_directory, data_dir = _roots(tmp_path)
    host_id = HostId.generate()
    store_dir = materialize_machine_store(latchkey_directory, data_dir, host_id)

    write_machine_credentials(store_dir, b"encrypted-4093", "2")

    assert (store_dir / ENCRYPTION_KEY_FILENAME).is_symlink()
    assert (store_dir / BROWSER_STATE_FILENAME).is_symlink()


def test_clear_machine_credentials_removes_only_the_mirror(tmp_path: Path) -> None:
    latchkey_directory, data_dir = _roots(tmp_path)
    host_id = HostId.generate()
    store_dir = materialize_machine_store(latchkey_directory, data_dir, host_id)
    write_machine_credentials(store_dir, b"encrypted-4093", "2")

    clear_machine_credentials(store_dir)

    assert not machine_credentials_path(data_dir, host_id).exists()
    assert (store_dir / UPSTREAM_DATA_FORMAT_VERSION_FILENAME).is_file()
    assert (store_dir / ENCRYPTION_KEY_FILENAME).is_symlink()


def test_clear_machine_credentials_is_a_no_op_without_a_mirror(tmp_path: Path) -> None:
    latchkey_directory, data_dir = _roots(tmp_path)
    store_dir = materialize_machine_store(latchkey_directory, data_dir, HostId.generate())

    clear_machine_credentials(store_dir)

    assert not (store_dir / CREDENTIALS_STORE_FILENAME).exists()


def test_a_machine_store_resolves_the_desktop_key_as_its_own(tmp_path: Path) -> None:
    """The mirror is held under the desktop's key, which is what lets the browser session be shared."""
    latchkey_directory, data_dir = _roots(tmp_path)
    desktop_key = load_or_create_encryption_key(latchkey_directory)

    store_dir = materialize_machine_store(latchkey_directory, data_dir, HostId.generate())

    assert load_or_create_encryption_key(store_dir) == desktop_key
