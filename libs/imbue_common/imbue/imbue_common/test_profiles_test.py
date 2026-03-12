from pathlib import Path

import pytest

from imbue.imbue_common.test_profiles import detect_branch
from imbue.imbue_common.test_profiles import load_profiles
from imbue.imbue_common.test_profiles import resolve_active_profile

_SAMPLE_CONFIG = """\
[profiles.mng]
branch_prefixes = ["mng/"]
testpaths = ["libs/mng", "libs/imbue_common"]
cov_packages = ["imbue.mng", "imbue.imbue_common"]

[profiles.minds]
branch_prefixes = ["minds/", "mind/"]
testpaths = ["apps/minds"]
cov_packages = ["imbue.minds"]
"""


class TestLoadProfiles:
    def test_returns_empty_for_missing_file(self, tmp_path: Path) -> None:
        result = load_profiles(tmp_path / "nonexistent.toml")
        assert result == ()

    def test_loads_profiles_from_toml(self, tmp_path: Path) -> None:
        config_path = tmp_path / "test_profiles.toml"
        config_path.write_text(_SAMPLE_CONFIG)

        result = load_profiles(config_path)

        assert len(result) == 2
        assert result[0].name == "mng"
        assert result[0].branch_prefixes == ("mng/",)
        assert result[0].testpaths == ("libs/mng", "libs/imbue_common")
        assert result[0].cov_packages == ("imbue.mng", "imbue.imbue_common")
        assert result[1].name == "minds"
        assert result[1].branch_prefixes == ("minds/", "mind/")

    def test_returns_empty_for_no_profiles_section(self, tmp_path: Path) -> None:
        config_path = tmp_path / "test_profiles.toml"
        config_path.write_text("[other]\nkey = 'value'\n")

        result = load_profiles(config_path)

        assert result == ()

    def test_profiles_are_frozen(self, tmp_path: Path) -> None:
        config_path = tmp_path / "test_profiles.toml"
        config_path.write_text(_SAMPLE_CONFIG)

        result = load_profiles(config_path)

        with pytest.raises(AttributeError):
            result[0].name = "changed"  # type: ignore[misc]


class TestDetectBranch:
    def test_uses_github_head_ref_for_prs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_HEAD_REF", "mng/fix-foo")
        assert detect_branch() == "mng/fix-foo"

    def test_uses_github_ref_name_for_pushes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.setenv("GITHUB_REF_NAME", "main")
        assert detect_branch() == "main"

    def test_prefers_github_head_ref_over_ref_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_HEAD_REF", "mng/fix")
        monkeypatch.setenv("GITHUB_REF_NAME", "main")
        assert detect_branch() == "mng/fix"

    def test_ignores_empty_github_head_ref(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_HEAD_REF", "")
        monkeypatch.setenv("GITHUB_REF_NAME", "main")
        assert detect_branch() == "main"

    def test_falls_back_to_git(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_REF_NAME", raising=False)

        branch = detect_branch()

        # We are in a git repo, so this should return a non-empty string
        assert branch is not None
        assert len(branch) > 0


class TestResolveActiveProfile:
    @staticmethod
    def _write_config(tmp_path: Path) -> Path:
        config_path = tmp_path / "test_profiles.toml"
        config_path.write_text(_SAMPLE_CONFIG)
        return tmp_path

    def test_explicit_all_disables_profiles(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo_root = self._write_config(tmp_path)
        monkeypatch.setenv("MNG_TEST_PROFILE", "all")

        assert resolve_active_profile(repo_root) is None

    def test_explicit_profile_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo_root = self._write_config(tmp_path)
        monkeypatch.setenv("MNG_TEST_PROFILE", "minds")

        result = resolve_active_profile(repo_root)

        assert result is not None
        assert result.name == "minds"

    def test_explicit_unknown_profile_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo_root = self._write_config(tmp_path)
        monkeypatch.setenv("MNG_TEST_PROFILE", "nonexistent")

        assert resolve_active_profile(repo_root) is None

    def test_branch_prefix_matching(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo_root = self._write_config(tmp_path)
        monkeypatch.delenv("MNG_TEST_PROFILE", raising=False)
        monkeypatch.setenv("GITHUB_HEAD_REF", "mng/fix-something")

        result = resolve_active_profile(repo_root)

        assert result is not None
        assert result.name == "mng"

    def test_second_prefix_matches(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo_root = self._write_config(tmp_path)
        monkeypatch.delenv("MNG_TEST_PROFILE", raising=False)
        monkeypatch.setenv("GITHUB_HEAD_REF", "mind/new-feature")

        result = resolve_active_profile(repo_root)

        assert result is not None
        assert result.name == "minds"

    def test_no_matching_branch_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo_root = self._write_config(tmp_path)
        monkeypatch.delenv("MNG_TEST_PROFILE", raising=False)
        monkeypatch.setenv("GITHUB_HEAD_REF", "feature/something-else")

        assert resolve_active_profile(repo_root) is None

    def test_no_config_file_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MNG_TEST_PROFILE", raising=False)

        assert resolve_active_profile(tmp_path) is None

    def test_first_matching_profile_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
