"""Verify the Dockerfile's `ARG OFFLOAD_VERSION` matches every offload version
reference in `.github/workflows/ci.yml`.

The Dockerfile's offload-builder stage compiles a particular offload version
and copies the binary into the final image so `offload apply-diff` works at
sandbox-prepare time. CI (`.github/workflows/ci.yml`) installs offload on
the runner with `cargo install offload@<version>`. The two pins must agree
or offload will fall back to a full image rebuild on every CI run, defeating
the checkpoint cache.

Mirrors `apps/minds/imbue/minds/test_claude_version_alignment.py`, which
guards the same kind of cross-file ARG pin for `CLAUDE_CODE_VERSION`.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[5]
_DOCKERFILE_PATH = _REPO_ROOT / "libs" / "mngr" / "imbue" / "mngr" / "resources" / "Dockerfile"
_CI_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _parse_dockerfile_offload_version(dockerfile_text: str) -> str:
    """Extract the OFFLOAD_VERSION default from the Dockerfile's `ARG` line.

    Accepts both quoted (`ARG OFFLOAD_VERSION="0.9.2"`) and unquoted
    (`ARG OFFLOAD_VERSION=0.9.2`) forms so future stylistic edits don't
    break the test.
    """
    match = re.search(
        r'^ARG\s+OFFLOAD_VERSION\s*=\s*"?([^"\s]+)"?\s*$',
        dockerfile_text,
        flags=re.MULTILINE,
    )
    assert match is not None, "Dockerfile is missing `ARG OFFLOAD_VERSION=...`."
    version = match.group(1)
    assert version, (
        "Dockerfile `ARG OFFLOAD_VERSION` default is empty; pin it to match "
        "the offload version in .github/workflows/ci.yml."
    )
    return version


def _find_ci_offload_versions(ci_text: str) -> list[tuple[str, str]]:
    """Return every (label, version) pair found in ci.yml referring to an
    offload version pin. Each label tags where the version came from so the
    failure message can point directly at the misaligned line.
    """
    pairs: list[tuple[str, str]] = []
    for cache_key in re.findall(r"cargo-offload-([0-9]+\.[0-9]+\.[0-9]+)-", ci_text):
        pairs.append(("cache key (cargo-offload-<ver>-)", cache_key))
    for grep_version in re.findall(r"offload --version \| grep -q '([0-9]+\.[0-9]+\.[0-9]+)'", ci_text):
        pairs.append(("offload --version grep", grep_version))
    for install_version in re.findall(r"cargo install offload@([0-9]+\.[0-9]+\.[0-9]+)", ci_text):
        pairs.append(("cargo install offload@<ver>", install_version))
    return pairs


def test_dockerfile_offload_version_matches_ci_workflow_pins() -> None:
    """Every offload version reference in ci.yml must equal the Dockerfile's
    `ARG OFFLOAD_VERSION` default.

    A mismatch would silently regress the apply-diff optimization the multi-
    stage build was added to enable -- offload would invoke `offload
    apply-diff` inside a sandbox built from a different version of the binary
    than the runner expects, fall back to a full rebuild, and waste the
    checkpoint cache.
    """
    dockerfile_version = _parse_dockerfile_offload_version(_DOCKERFILE_PATH.read_text())
    ci_versions = _find_ci_offload_versions(_CI_WORKFLOW_PATH.read_text())
    assert ci_versions, (
        f"Found no offload version references in {_CI_WORKFLOW_PATH}; the "
        f"regex patterns in this test are stale. Update them to match the "
        f"current ci.yml."
    )
    mismatches = [(label, version) for label, version in ci_versions if version != dockerfile_version]
    assert not mismatches, (
        f"Dockerfile OFFLOAD_VERSION={dockerfile_version!r} does not match "
        f"the following ci.yml references:\n"
        + "\n".join(f"  - {label}: {version!r}" for label, version in mismatches)
        + f"\nBump one of them to match the other. See {_DOCKERFILE_PATH} and {_CI_WORKFLOW_PATH}."
    )
