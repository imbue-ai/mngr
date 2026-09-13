import os
import time
from pathlib import Path

from imbue.mngr_imbue_cloud.slices.box_known_hosts import BOX_KNOWN_HOSTS_FILE_PREFIX
from imbue.mngr_imbue_cloud.slices.box_known_hosts import BOX_KNOWN_HOSTS_MAX_AGE_SECONDS
from imbue.mngr_imbue_cloud.slices.box_known_hosts import remove_box_known_hosts_file
from imbue.mngr_imbue_cloud.slices.box_known_hosts import sweep_stale_box_known_hosts_files
from imbue.mngr_imbue_cloud.slices.box_known_hosts import write_box_known_hosts_file

_BOX_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + "A" * 20


def test_write_pins_the_box_endpoint_in_a_fresh_file_per_call(tmp_path: Path) -> None:
    # Two commands to the same box must not share a file: one finishing would
    # unlink the file the other's ssh is about to read.
    first = write_box_known_hosts_file(tmp_path, "box.example", 22, _BOX_KEY)
    second = write_box_known_hosts_file(tmp_path, "box.example", 22, _BOX_KEY)
    assert first != second
    assert first.name.startswith(BOX_KNOWN_HOSTS_FILE_PREFIX)
    assert first.read_text().startswith("box.example ssh-ed25519 ")
    remove_box_known_hosts_file(first)
    remove_box_known_hosts_file(first)
    assert not first.exists() and second.exists()


def test_sweep_removes_only_stale_box_files(tmp_path: Path) -> None:
    stale = write_box_known_hosts_file(tmp_path, "box.example", 22, _BOX_KEY)
    fresh = write_box_known_hosts_file(tmp_path, "box.example", 22, _BOX_KEY)
    unrelated = tmp_path / "ssh_id"
    unrelated.write_text("not a known_hosts file")
    old_mtime = time.time() - BOX_KNOWN_HOSTS_MAX_AGE_SECONDS - 60
    os.utime(stale, (old_mtime, old_mtime))
    os.utime(unrelated, (old_mtime, old_mtime))
    assert sweep_stale_box_known_hosts_files(tmp_path) == 1
    assert not stale.exists() and fresh.exists() and unrelated.exists()
    # The writer sweeps as a side effect, so a killed process's leftovers never outlive a day of use.
    os.utime(fresh, (old_mtime, old_mtime))
    newest = write_box_known_hosts_file(tmp_path, "box.example", 22, _BOX_KEY)
    assert not fresh.exists() and newest.exists()
