from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.imbue_common.test_profiles import detect_branch
from imbue.imbue_common.test_profiles import load_profiles
from imbue.imbue_common.test_profiles import resolve_active_profile

_SAMPLE_CONFIG = """\
[profiles.mng]
branch_prefixes = ["mng-only/"]
testpaths = ["libs/mng", "libs/imbue_common"]
cov_packages = ["imbue.mng", "imbue.imbue_common"]

[profiles.minds]
branch_prefixes = ["minds-only/", "mind-only/"]
testpaths = ["apps/minds"]
cov_packages = ["imbue.minds"]
"""


def _write_sample_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "test_profiles.toml"
    config_path.write_text(_SAMPLE_CONFIG)
    return tmp_path


# -- load_profiles -----------------------------------------------------------


def test_load_profiles_returns_empty_for_missing_file(tmp_path: Path) -> None:
    result = load_profiles(tmp_path / "nonexistent.toml")
    assert result == ()


def test_load_profiles_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "test_profiles.toml"
    config_path.write_text(_SAMPLE_CONFIG)

    result = load_profiles(config_path)

    assert len(result) == 2
    assert result[0].name == "mng"
    assert result[0].branch_prefixes == ("mng-only/",)
    assert result[0].testpaths == ("libs/mng", "libs/imbue_common")
    assert result[0].cov_packages == ("imbue.mng", "imbue.imbue_common")
    assert result[1].name == "minds"
    assert result[1].branch_prefixes == ("minds-only/", "mind-only/")


def test_load_profiles_returns_empty_for_no_profiles_section(tmp_path: Path) -> None:
    config_path = tmp_path / "test_profiles.toml"
    config_path.write_text("[other]\nkey = 'value'\n")

    result = load_profiles(config_path)

    assert result == ()


def test_load_profiles_are_frozen(tmp_path: Path) -> None:
    config_path = tmp_path / "test_profiles.toml"
    config_path.write_text(_SAMPLE_CONFIG)

    result = load_profiles(config_path)

    with pytest.raises(ValidationError):
        result[0].name = "changed"  # type: ignore[misc]


# -- detect_branch -----------------------------------------------------------


def test_detect_branch_uses_github_head_ref_for_prs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_HEAD_REF", "mng-only/fix-foo")
    assert detect_branch() == "mng-only/fix-foo"


def test_detect_branch_uses_github_ref_name_for_pushes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    assert detect_branch() == "main"


def test_detect_branch_prefers_github_head_ref_over_ref_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_HEAD_REF", "mng-only/fix")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    assert detect_branch() == "mng-only/fix"


def test_detect_branch_ignores_empty_github_head_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_HEAD_REF", "")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    assert detect_branch() == "main"


def test_detect_branch_falls_back_to_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)

    branch = detect_branch()

    # We are in a git repo, so this should return a non-empty string
    assert branch is not None
    assert len(branch) > 0


# -- resolve_active_profile ---------------------------------------------------


def test_resolve_explicit_all_disables_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = _write_sample_config(tmp_path)
    monkeypatch.setenv("MNG_TEST_PROFILE", "all")

    assert resolve_active_profile(repo_root) is None


def test_resolve_explicit_profile_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = _write_sample_config(tmp_path)
    monkeypatch.setenv("MNG_TEST_PROFILE", "minds")

    result = resolve_active_profile(repo_root)

    assert result is not None
    assert result.name == "minds"


def test_resolve_explicit_unknown_profile_warns_and_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _write_sample_config(tmp_path)
    monkeypatch.setenv("MNG_TEST_PROFILE", "nonexistent")

    with pytest.warns(UserWarning, match="does not match any profile"):
        assert resolve_active_profile(repo_root) is None


def test_resolve_branch_prefix_matching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = _write_sample_config(tmp_path)
    monkeypatch.delenv("MNG_TEST_PROFILE", raising=False)
    monkeypatch.setenv("GITHUB_HEAD_REF", "mng-only/fix-something")

    result = resolve_active_profile(repo_root)

    assert result is not None
    assert result.name == "mng"


def test_resolve_second_prefix_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = _write_sample_config(tmp_path)
    monkeypatch.delenv("MNG_TEST_PROFILE", raising=False)
    monkeypatch.setenv("GITHUB_HEAD_REF", "mind-only/new-feature")

    result = resolve_active_profile(repo_root)

    assert result is not None
    assert result.name == "minds"


def test_resolve_no_matching_branch_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = _write_sample_config(tmp_path)
    monkeypatch.delenv("MNG_TEST_PROFILE", raising=False)
    monkeypatch.setenv("GITHUB_HEAD_REF", "feature/something-else")

    assert resolve_active_profile(repo_root) is None


def test_resolve_no_config_file_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNG_TEST_PROFILE", raising=False)

    assert resolve_active_profile(tmp_path) is None


def test_resolve_first_matching_profile_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "test_profiles.toml"
    config_path.write_text(
        """\
[profiles.alpha]
branch_prefixes = ["feat/"]
testpaths = ["libs/a"]
cov_packages = ["a"]

[profiles.beta]
branch_prefixes = ["feat/"]
testpaths = ["libs/b"]
cov_packages = ["b"]
"""
    )
    monkeypatch.delenv("MNG_TEST_PROFILE", raising=False)
    monkeypatch.setenv("GITHUB_HEAD_REF", "feat/x")

    result = resolve_active_profile(tmp_path)

    assert result is not None
    assert result.name == "alpha"
