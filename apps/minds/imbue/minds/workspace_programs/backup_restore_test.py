from pathlib import Path

from imbue.minds.workspace_programs.backup_restore import backup_root_for_host_dir


def test_backup_root_of_a_host_dir_symlinked_out_of_the_home_tree_is_the_home_tree(tmp_path: Path) -> None:
    home = tmp_path / "home" / "user"
    home.mkdir(parents=True)
    volume = tmp_path / "host_volume"
    volume.mkdir()
    (home / ".mngr").symlink_to(volume)

    assert backup_root_for_host_dir(str(home / ".mngr")) == str(home.resolve())


def test_backup_root_of_a_home_tree_symlinked_onto_a_volume_is_the_volume_home(tmp_path: Path) -> None:
    volume_home = tmp_path / "mngr-vol" / "home"
    (volume_home / ".mngr").mkdir(parents=True)
    home = tmp_path / "user"
    home.symlink_to(volume_home)

    assert backup_root_for_host_dir(str(home / ".mngr")) == str(volume_home.resolve())


def test_backup_root_of_a_legacy_host_dir_is_the_host_dir_itself(tmp_path: Path) -> None:
    volume = tmp_path / "host_volume"
    volume.mkdir()
    legacy_host_dir = tmp_path / "mngr"
    legacy_host_dir.symlink_to(volume)

    assert backup_root_for_host_dir(str(legacy_host_dir)) == str(volume.resolve())
