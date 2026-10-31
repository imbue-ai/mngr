import re
from pathlib import Path
from uuid import uuid4

from imbue.minds.mngr_settings.profile_paths import ensure_active_profile_dir
from imbue.minds.mngr_settings.profile_paths import read_active_profile_dir

_CREATED_CONFIG_PATTERN = re.compile(r'profile = "([0-9a-f]{32})"\n')


def test_ensure_active_profile_dir_creates_the_config_and_profile_dir_in_a_fresh_host_dir(tmp_path: Path) -> None:
    mngr_host_dir = tmp_path / "mngr"

    ensure_active_profile_dir(mngr_host_dir)

    match = _CREATED_CONFIG_PATTERN.fullmatch((mngr_host_dir / "config.toml").read_text())
    assert match is not None
    profile_dir = mngr_host_dir / "profiles" / match.group(1)
    assert profile_dir.is_dir()
    assert read_active_profile_dir(mngr_host_dir) == profile_dir


def test_ensure_active_profile_dir_reuses_the_profile_the_config_names(tmp_path: Path) -> None:
    mngr_host_dir = tmp_path / "mngr"
    mngr_host_dir.mkdir()
    profile_id = uuid4().hex
    config_text = f'# kept-48213\nprofile = "{profile_id}"\n'
    (mngr_host_dir / "config.toml").write_text(config_text)

    ensure_active_profile_dir(mngr_host_dir)

    assert (mngr_host_dir / "config.toml").read_text() == config_text
    assert (mngr_host_dir / "profiles" / profile_id).is_dir()
    assert [path.name for path in (mngr_host_dir / "profiles").iterdir()] == [profile_id]


def test_ensure_active_profile_dir_replaces_a_config_that_names_no_profile(tmp_path: Path) -> None:
    mngr_host_dir = tmp_path / "mngr"
    mngr_host_dir.mkdir()
    (mngr_host_dir / "config.toml").write_text('profile = ""\nother = "dropped-60317"\n')

    ensure_active_profile_dir(mngr_host_dir)

    match = _CREATED_CONFIG_PATTERN.fullmatch((mngr_host_dir / "config.toml").read_text())
    assert match is not None
    assert read_active_profile_dir(mngr_host_dir) == mngr_host_dir / "profiles" / match.group(1)


def test_ensure_active_profile_dir_leaves_an_unreadable_config_untouched(tmp_path: Path) -> None:
    mngr_host_dir = tmp_path / "mngr"
    mngr_host_dir.mkdir()
    (mngr_host_dir / "config.toml").write_text("{not toml-95120")

    ensure_active_profile_dir(mngr_host_dir)

    assert (mngr_host_dir / "config.toml").read_text() == "{not toml-95120"
    assert not (mngr_host_dir / "profiles").exists()
