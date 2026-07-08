import json
from collections.abc import Generator
from pathlib import Path

import pytest

from imbue.minds import build_info


@pytest.fixture(autouse=True)
def _clear_build_info_caches() -> Generator[None, None, None]:
    # resolve_release_id / resolve_git_sha are @cache'd, so each test must start
    # and end with a cleared cache to observe its own env changes.
    build_info.resolve_release_id.cache_clear()
    build_info.resolve_git_sha.cache_clear()
    yield
    build_info.resolve_release_id.cache_clear()
    build_info.resolve_git_sha.cache_clear()


def test_resolve_release_id_prefers_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(build_info.RELEASE_ID_ENV_VAR, "9.9.9")
    assert build_info.resolve_release_id() == "9.9.9"


def test_resolve_release_id_falls_back_to_package_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(build_info.RELEASE_ID_ENV_VAR, raising=False)
    package_json = Path(build_info.__file__).resolve().parents[2] / "package.json"
    expected_version = json.loads(package_json.read_text())["version"]
    assert build_info.resolve_release_id() == expected_version


def test_resolve_git_sha_prefers_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(build_info.GIT_SHA_ENV_VAR, "abc1234deadbeef")
    assert build_info.resolve_git_sha() == "abc1234deadbeef"


def test_resolve_git_sha_falls_back_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(build_info.GIT_SHA_ENV_VAR, raising=False)
    assert build_info.resolve_git_sha() == build_info.UNKNOWN_GIT_SHA
