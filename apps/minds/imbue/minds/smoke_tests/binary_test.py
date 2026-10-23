from pathlib import Path

import pytest

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.smoke_tests.backend import resolve_smoke_backend
from imbue.minds.smoke_tests.backend import resolve_smoke_client_config_path
from imbue.minds.smoke_tests.binary import app_environment
from imbue.minds.smoke_tests.binary import app_log_dir
from imbue.minds.smoke_tests.binary import bundled_root_name_or_none
from imbue.minds.smoke_tests.binary import cached_build_dir
from imbue.minds.smoke_tests.binary import is_studio_binary_selected
from imbue.minds.smoke_tests.binary import launch_target_for
from imbue.minds.smoke_tests.binary import packaged_studio_binary
from imbue.minds.smoke_tests.binary import published_build_cache_key
from imbue.minds.smoke_tests.binary import published_build_cache_root
from imbue.minds.smoke_tests.binary import published_build_layout_or_none
from imbue.minds.smoke_tests.binary import published_channel_studio_binary
from imbue.minds.smoke_tests.binary import resolve_studio_binary
from imbue.minds.smoke_tests.data_types import SmokeBackend
from imbue.minds.smoke_tests.data_types import StudioBinaryKind
from imbue.minds.smoke_tests.primitives import ReleaseChannelName
from imbue.minds.smoke_tests.primitives import SMOKE_BINARY_PATH_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_CHANNEL_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_DEV_CHECKOUT_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeConfigError

_BUNDLED_ROOT_NAME_RELATIVE_PATH = Path("pyproject/imbue/minds/config/envs/_bundled/root_name")


def _staging_backend() -> SmokeBackend:
    env_name = DevEnvName("staging")
    return resolve_smoke_backend(env_name, resolve_smoke_client_config_path(env_name))


def _write_linux_build(build_dir: Path, bundled_root_name: str | None) -> Path:
    """Lay out ``<build>/opt/Imbue Studio/imbue-studio`` the way the unpacked ``.deb`` does."""
    app_dir = build_dir / "opt" / "Imbue Studio"
    app_dir.mkdir(parents=True)
    executable = app_dir / "imbue-studio"
    executable.write_text("#!/bin/sh\n")
    if bundled_root_name is not None:
        root_name_file = app_dir / "resources" / _BUNDLED_ROOT_NAME_RELATIVE_PATH
        root_name_file.parent.mkdir(parents=True)
        root_name_file.write_text(bundled_root_name + "\n")
    return executable


def test_nothing_selected_names_the_three_ways_to_name_a_build() -> None:
    assert is_studio_binary_selected({"PATH": "/usr/bin"}) is False
    with pytest.raises(SmokeConfigError) as excinfo:
        resolve_studio_binary(_staging_backend(), {"PATH": "/usr/bin"})
    for env_var in (SMOKE_CHANNEL_ENV_VAR, SMOKE_BINARY_PATH_ENV_VAR, SMOKE_DEV_CHECKOUT_ENV_VAR):
        assert env_var in str(excinfo.value)


def test_an_explicit_executable_wins_over_the_dev_checkout_and_the_channel(tmp_path: Path) -> None:
    executable = _write_linux_build(tmp_path, bundled_root_name="minds-staging")
    environ = {
        SMOKE_BINARY_PATH_ENV_VAR: str(executable),
        SMOKE_DEV_CHECKOUT_ENV_VAR: "1",
        SMOKE_CHANNEL_ENV_VAR: "alpha",
    }
    assert is_studio_binary_selected(environ) is True

    binary = resolve_studio_binary(_staging_backend(), environ)

    assert binary.kind == StudioBinaryKind.PACKAGED
    assert binary.executable == executable
    assert binary.main_js is None
    assert str(binary.root_name) == "minds-staging"


def test_a_packaged_build_baked_for_another_tier_is_refused(tmp_path: Path) -> None:
    executable = _write_linux_build(tmp_path, bundled_root_name="minds")
    with pytest.raises(SmokeConfigError, match="baked for the 'production' tier"):
        packaged_studio_binary(executable, _staging_backend(), label="a production build")


def test_a_packaged_build_without_a_bundled_root_name_runs_as_the_backend_s_tier(tmp_path: Path) -> None:
    executable = _write_linux_build(tmp_path, bundled_root_name=None)
    binary = packaged_studio_binary(executable, _staging_backend(), label="an unbundled build")
    assert str(binary.root_name) == "minds-staging"
    assert str(binary.label) == "an unbundled build"


def test_a_missing_executable_is_refused_by_path(tmp_path: Path) -> None:
    with pytest.raises(SmokeConfigError, match="does not exist"):
        resolve_studio_binary(_staging_backend(), {SMOKE_BINARY_PATH_ENV_VAR: str(tmp_path / "absent")})


def test_bundled_root_name_is_read_from_the_linux_and_macos_layouts(tmp_path: Path) -> None:
    linux_executable = _write_linux_build(tmp_path / "linux", bundled_root_name="minds-staging")
    assert bundled_root_name_or_none(linux_executable) == "minds-staging"

    macos_executable = tmp_path / "Imbue Studio.app" / "Contents" / "MacOS" / "Imbue Studio"
    macos_executable.parent.mkdir(parents=True)
    macos_executable.write_text("")
    macos_root_name_file = tmp_path / "Imbue Studio.app" / "Contents" / "Resources" / _BUNDLED_ROOT_NAME_RELATIVE_PATH
    macos_root_name_file.parent.mkdir(parents=True)
    macos_root_name_file.write_text("minds\n")
    assert bundled_root_name_or_none(macos_executable) == "minds"

    assert bundled_root_name_or_none(_write_linux_build(tmp_path / "bare", bundled_root_name=None)) is None


def test_a_channel_cannot_be_tested_against_a_tier_without_a_feed() -> None:
    with pytest.raises(SmokeConfigError):
        published_channel_studio_binary(ReleaseChannelName("alpha"), _staging_backend())


def test_the_published_build_layout_follows_what_the_feed_publishes() -> None:
    linux = published_build_layout_or_none("linux", "x86_64")
    assert linux is not None
    assert str(linux.platform) == "linux"
    assert linux.executable_relative_path == Path("opt/Imbue Studio/imbue-studio")

    mac = published_build_layout_or_none("darwin", "arm64")
    assert mac is not None
    assert str(mac.platform) == "mac"
    assert mac.executable_relative_path == Path("Imbue Studio.app/Contents/MacOS/Imbue Studio")

    assert published_build_layout_or_none("darwin", "x86_64") is None
    assert published_build_layout_or_none("linux", "aarch64") is None


def test_the_launch_target_of_a_packaged_build_has_no_main_script(tmp_path: Path) -> None:
    executable = _write_linux_build(tmp_path, bundled_root_name="minds-staging")
    target = launch_target_for(packaged_studio_binary(executable, _staging_backend(), label="packaged"))
    assert target.executable == executable
    assert target.main_js is None


def test_the_app_logs_under_the_tier_directory_of_the_data_home(tmp_path: Path) -> None:
    assert app_log_dir(tmp_path, "minds") == tmp_path / "production" / "logs"
    assert app_log_dir(tmp_path, "minds-staging") == tmp_path / "staging" / "logs"


def test_the_app_runs_on_the_shell_s_environment_with_this_test_process_s_own_taken_out() -> None:
    environ = {
        "HOME": "/home/runner",
        "PATH": "/tmp/pytest_resource_guards_abc:/usr/bin:/code/.venv/bin",
        "TMPDIR": "/tmp",
        "LANG": "en_US.UTF-8",
        "DISPLAY": ":99",
        "PYTEST_CURRENT_TEST": "apps/minds/smoke_tests/test_smoke_journey.py::test_it (call)",
        "_PYTEST_GUARD_DOCKER": "block",
        "_PYTEST_GUARD_PHASE": "call",
        "MNGR_HOST_DIR": "/home/runner/.mngr-test-1234",
        "MNGR_ROOT_NAME": "mngr-test-1234",
        "MINDS_ROOT_NAME": "minds-ci-snapshot",
        "MINDS_DATA_HOME": "/home/runner/.minds-e2e",
        "UV_OFFLINE": "1",
        "UV_FROZEN": "1",
        "VIRTUAL_ENV": "/code/.venv",
        "_PYTEST_GUARD_WRAPPER_DIR": "/tmp/pytest_resource_guards_abc",
    }
    launch_env = {"MINDS_DATA_HOME": "/tmp/run/studio-data-home", "MINDS_ROOT_NAME": "minds-staging"}

    app_env = app_environment(environ, launch_env)

    assert app_env == {
        "HOME": "/home/runner",
        "PATH": "/usr/bin:/code/.venv/bin",
        "TMPDIR": "/tmp",
        "LANG": "en_US.UTF-8",
        "DISPLAY": ":99",
        "MINDS_DATA_HOME": "/tmp/run/studio-data-home",
        "MINDS_ROOT_NAME": "minds-staging",
    }


def test_the_cache_root_is_a_user_level_directory_no_temp_sweep_prunes(tmp_path: Path) -> None:
    home = tmp_path / "home"

    mac = published_build_cache_root("darwin", home, xdg_cache_home=None)
    assert mac == home / "Library" / "Caches" / "imbue-studio-smoke" / "builds"

    linux = published_build_cache_root("linux", home, xdg_cache_home=None)
    assert linux == home / ".cache" / "imbue-studio-smoke" / "builds"

    honors_xdg = published_build_cache_root("linux", home, xdg_cache_home=tmp_path / "xdg")
    assert honors_xdg == tmp_path / "xdg" / "imbue-studio-smoke" / "builds"


def test_the_cache_key_changes_when_the_published_bytes_change() -> None:
    layout = published_build_layout_or_none("darwin", "arm64")
    assert layout is not None
    rebuilt = published_build_cache_key(
        ReleaseChannelName("alpha"), NonEmptyStr("0.8.6"), layout, NonEmptyStr("Zm9vYmFy")
    )
    original = published_build_cache_key(
        ReleaseChannelName("alpha"), NonEmptyStr("0.8.6"), layout, NonEmptyStr("YmFyZm9v")
    )

    # Same channel and version, different bytes: a republished build never reuses the old tree.
    assert rebuilt != original
    assert str(rebuilt).startswith("alpha-0.8.6-macos-arm64-")


def test_a_cache_miss_populates_once_and_a_hit_does_not(tmp_path: Path) -> None:
    populate_calls: list[Path] = []

    def populate(destination: Path) -> None:
        populate_calls.append(destination)
        (destination / "Studio").write_text("binary")

    first = cached_build_dir(tmp_path, NonEmptyStr("alpha-0.8.6-macos-arm64-abcd"), populate)
    second = cached_build_dir(tmp_path, NonEmptyStr("alpha-0.8.6-macos-arm64-abcd"), populate)

    assert first == second == tmp_path / "alpha-0.8.6-macos-arm64-abcd"
    assert (first / "Studio").read_text() == "binary"
    assert len(populate_calls) == 1
    # Populated somewhere else and published by rename, so a reader never sees a half-written tree.
    assert populate_calls[0] != first


def test_a_failed_populate_publishes_nothing_so_the_next_run_retries(tmp_path: Path) -> None:
    def failing_populate(destination: Path) -> None:
        (destination / "half-written").write_text("partial")
        raise SmokeConfigError("the download died")

    with pytest.raises(SmokeConfigError):
        cached_build_dir(tmp_path, NonEmptyStr("alpha-0.8.6-macos-arm64-abcd"), failing_populate)

    assert not (tmp_path / "alpha-0.8.6-macos-arm64-abcd").exists()
    # Nor any scratch directory: a half-written tree must not survive to be mistaken for a build.
    assert [path.name for path in tmp_path.iterdir() if path.name.startswith("alpha-")] == []
