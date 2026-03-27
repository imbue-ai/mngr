"""Test profile support for selectively running tests based on branch name.

When the current git branch matches a profile's branch_prefixes, only tests
from the profile's testpaths are collected, and coverage is limited to the
profile's cov_packages.

Profiles are defined in test_profiles.toml at the repository root.

Environment variables:
- MNGR_TEST_PROFILE: Force a specific profile (overrides branch detection).
  Set to "all" to disable profile filtering entirely.
"""

import os
import subprocess
import tomllib
import warnings
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel


class ScopedProfile(FrozenModel):
    """A named test profile that restricts which tests and coverage packages are active."""

    name: str
    branch_prefixes: tuple[str, ...]
    testpaths: tuple[str, ...]
    cov_packages: tuple[str, ...]


_CONFIG_FILENAME: Final[str] = "test_profiles.toml"


def load_profiles(config_path: Path) -> tuple[ScopedProfile, ...]:
    """Load test profiles from a TOML config file.

    Returns an empty tuple if the file does not exist.
    Raises on malformed TOML or missing required fields (fail-fast).
    """
    if not config_path.exists():
        return ()

    with config_path.open("rb") as f:
        config = tomllib.load(f)

    profiles: list[ScopedProfile] = []
    for name, data in config.get("profiles", {}).items():
        profiles.append(
            ScopedProfile(
                name=name,
                branch_prefixes=tuple(data["branch_prefixes"]),
                testpaths=tuple(data["testpaths"]),
                cov_packages=tuple(data["cov_packages"]),
            )
        )
    return tuple(profiles)


def detect_branch() -> str | None:
    """Detect the current git branch name.

    Checks GitHub Actions environment variables first (GITHUB_HEAD_REF for PRs,
    GITHUB_REF_NAME for pushes), then falls back to git rev-parse.

    Returns None if the branch cannot be determined.
    """
    # GitHub Actions: PR source branch
    branch = os.environ.get("GITHUB_HEAD_REF", "")
    if branch:
        return branch

    # GitHub Actions: push target branch
    branch = os.environ.get("GITHUB_REF_NAME", "")
    if branch:
        return branch

    # Local: ask git
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


def resolve_active_profile(repo_root: Path) -> ScopedProfile | None:
    """Determine which test profile (if any) should be active.

    Resolution order:
    1. MNGR_TEST_PROFILE env var (explicit override)
       - "all" disables profile filtering
       - Any other value must match a profile name exactly
    2. Branch name matched against each profile's branch_prefixes (first match wins)

    Returns None if no profile is active (all tests run).
    """
    override = os.environ.get("MNGR_TEST_PROFILE", "")
    if override == "all":
        return None

    config_path = repo_root / _CONFIG_FILENAME
    profiles = load_profiles(config_path)
    if not profiles:
        return None

    # Explicit profile name override
    if override:
        for profile in profiles:
            if profile.name == override:
                return profile
        available = ", ".join(p.name for p in profiles)
        warnings.warn(
            f"MNGR_TEST_PROFILE='{override}' does not match any profile in {_CONFIG_FILENAME}. "
            f"Available profiles: {available}. Running all tests.",
            stacklevel=1,
        )
        return None

    # Branch-based profile detection
    branch = detect_branch()
    if branch is None:
        return None

    for profile in profiles:
        for prefix in profile.branch_prefixes:
            if branch.startswith(prefix):
                return profile

    return None
