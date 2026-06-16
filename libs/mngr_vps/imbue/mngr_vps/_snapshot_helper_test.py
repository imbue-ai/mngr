"""Unit tests for the snapshot_helper.* resources and their loader."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from imbue.mngr_vps.container_setup import load_resource_text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
def test_snapshot_helper_script_passes_bash_syntax_check(tmp_path: Path) -> None:
    """`bash -n` must accept the bundled snapshot_helper.sh as valid syntax.

    Also exercises the wheel's resource bundling end-to-end: if the
    pyproject.toml `include` directive ever drops the .sh file, `load_resource_text`
    raises before we even get to the bash check.
    """
    script_path = tmp_path / "snapshot_helper.sh"
    script_path.write_text(load_resource_text("snapshot_helper.sh"))
    result = subprocess.run(
        ["bash", "-n", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_snapshot_helper_unit_loads_with_expected_systemd_directives() -> None:
    """The bundled systemd unit loads with real content (pyproject `include` smoke test).

    Combined with the bash-syntax test above, this ensures both resource
    files survive the wheel build. Asserting on the content (not merely that
    the load did not raise) catches an empty or wrong file that still loads.
    """
    content = load_resource_text("snapshot_helper.service")
    assert content.strip(), "snapshot_helper.service resource loaded empty"
    assert "[Unit]" in content
    assert "ExecStart=/usr/local/sbin/snapshot_helper.sh" in content


_FAKE_BTRFS = """#!/usr/bin/env bash
echo "$@" >> "$BTRFS_LOG"
exit 0
"""

# A no-op stand-in for inotifywait: exits immediately so the helper's tail
# pipeline ends and the script returns after processing the startup request.
_FAKE_INOTIFYWAIT = """#!/usr/bin/env bash
exit 0
"""


def _run_helper_once(
    tmp_path: Path,
    request: dict[str, object],
) -> dict[str, object]:
    """Run snapshot_helper.sh once against `request`, return the parsed result.json.

    Fakes `btrfs` (records its args to BTRFS_LOG, always succeeds) and
    `inotifywait` (exits immediately) so the script processes the
    already-present request.json at startup and then exits cleanly.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("btrfs", _FAKE_BTRFS), ("inotifywait", _FAKE_INOTIFYWAIT)):
        fake = bin_dir / name
        fake.write_text(body)
        fake.chmod(0o755)

    btrfs_mount = tmp_path / "mngr-btrfs"
    trigger_dir = tmp_path / "trigger"
    trigger_dir.mkdir(parents=True)
    (trigger_dir / "request.json").write_text(json.dumps(request))

    script_path = tmp_path / "snapshot_helper.sh"
    script_path.write_text(load_resource_text("snapshot_helper.sh"))

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["BTRFS_LOG"] = str(tmp_path / "btrfs.log")
    env["MNGR_BTRFS_MOUNT_PATH"] = str(btrfs_mount)
    env["MNGR_HOST_SUBVOLUME"] = str(btrfs_mount / "abcdef")
    env["MNGR_TRIGGER_DIR"] = str(trigger_dir)

    subprocess.run(
        ["bash", str(script_path)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30.0,
    )
    return json.loads((trigger_dir / "result.json").read_text())


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="bash and jq required",
)
def test_snapshot_helper_snapshot_creates_at_request_id_named_path(
    tmp_path: Path,
) -> None:
    """A `snapshot` op snapshots into snapshots/<request_id>, not a fixed path."""
    name = "2026-06-12T03:43:57.123456Z"
    result = _run_helper_once(tmp_path, {"request_id": name, "operation": "snapshot"})
    expected = str(tmp_path / "mngr-btrfs" / "snapshots" / name)
    assert result["exit_code"] == 0
    assert result["snapshot_path"] == expected
    btrfs_calls = (tmp_path / "btrfs.log").read_text()
    assert f"subvolume snapshot -r {tmp_path / 'mngr-btrfs' / 'abcdef'} {expected}" in (btrfs_calls)


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="bash and jq required",
)
def test_snapshot_helper_snapshot_existing_path_fails_without_overwrite(
    tmp_path: Path,
) -> None:
    """A snapshot request whose target path already exists fails, never reusing it.

    This is the core invariant of the fix: paths are never reused across a
    delete+recreate, so a pre-existing snapshots/<name> must be refused rather
    than deleted+recreated. btrfs must not be invoked.
    """
    name = "2026-06-12T03:43:57.123456Z"
    (tmp_path / "mngr-btrfs" / "snapshots" / name).mkdir(parents=True)
    result = _run_helper_once(tmp_path, {"request_id": name, "operation": "snapshot"})
    assert result["exit_code"] == 1
    stderr = result["stderr"]
    assert isinstance(stderr, str)
    assert "already exists" in stderr
    assert not (tmp_path / "btrfs.log").exists()


# A btrfs stand-in that, unlike _FAKE_BTRFS, actually materializes the snapshot
# target dir (and removes it on delete) so a re-processed request hits the real
# "target already exists" path -- the exact condition the idempotency guard
# defends against.
_FAKE_BTRFS_MATERIALIZING = """#!/usr/bin/env bash
echo "$@" >> "$BTRFS_LOG"
case "$2" in
    snapshot) mkdir -p "${@: -1}" ;;
    delete)   rm -rf "${@: -1}" ;;
esac
exit 0
"""


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="bash and jq required",
)
def test_snapshot_helper_does_not_reprocess_already_serviced_request(
    tmp_path: Path,
) -> None:
    """Running the helper twice on the same un-consumed request.json keeps the success.

    This is the idempotency guard: a helper restart re-runs whatever request is
    still on disk via the startup path. Without the guard the second pass would
    find the snapshot path already present and clobber the good result.json with
    an "already exists" failure. With it, the second pass is a no-op: result.json
    still reports success and btrfs is invoked exactly once.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("btrfs", _FAKE_BTRFS_MATERIALIZING), ("inotifywait", _FAKE_INOTIFYWAIT)):
        fake = bin_dir / name
        fake.write_text(body)
        fake.chmod(0o755)

    btrfs_mount = tmp_path / "mngr-btrfs"
    trigger_dir = tmp_path / "trigger"
    trigger_dir.mkdir(parents=True)
    snapshot_name = "2026-06-12T03:43:57.123456Z"
    (trigger_dir / "request.json").write_text(json.dumps({"request_id": snapshot_name, "operation": "snapshot"}))

    script_path = tmp_path / "snapshot_helper.sh"
    script_path.write_text(load_resource_text("snapshot_helper.sh"))

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["BTRFS_LOG"] = str(tmp_path / "btrfs.log")
    env["MNGR_BTRFS_MOUNT_PATH"] = str(btrfs_mount)
    env["MNGR_HOST_SUBVOLUME"] = str(btrfs_mount / "abcdef")
    env["MNGR_TRIGGER_DIR"] = str(trigger_dir)

    for _run_index in range(2):
        subprocess.run(
            ["bash", str(script_path)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30.0,
        )

    result = json.loads((trigger_dir / "result.json").read_text())
    assert result["exit_code"] == 0
    assert result["request_id"] == snapshot_name
    assert result["snapshot_path"] == str(btrfs_mount / "snapshots" / snapshot_name)
    # The snapshot ran on the first pass only; the second pass was suppressed.
    btrfs_snapshot_calls = [
        line for line in (tmp_path / "btrfs.log").read_text().splitlines() if "subvolume snapshot" in line
    ]
    assert len(btrfs_snapshot_calls) == 1


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="bash and jq required",
)
def test_snapshot_helper_snapshot_rejects_unsafe_name(tmp_path: Path) -> None:
    """A snapshot request with an unsafe name is refused before btrfs runs."""
    result = _run_helper_once(tmp_path, {"request_id": "..", "operation": "snapshot"})
    assert result["exit_code"] == 2
    stderr = result["stderr"]
    assert isinstance(stderr, str)
    assert "invalid snapshot name" in stderr
    assert not (tmp_path / "btrfs.log").exists()


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="bash and jq required",
)
def test_snapshot_helper_cleanup_rejects_path_traversal_target(tmp_path: Path) -> None:
    """A cleanup target that escapes the snapshots dir is refused, btrfs never runs."""
    result = _run_helper_once(
        tmp_path,
        {"request_id": "req1", "operation": "cleanup", "target": "../evil"},
    )
    assert result["exit_code"] == 2
    stderr = result["stderr"]
    assert isinstance(stderr, str)
    assert "invalid cleanup target" in stderr
    assert not (tmp_path / "btrfs.log").exists()


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="bash and jq required",
)
def test_snapshot_helper_cleanup_absent_snapshot_is_success(tmp_path: Path) -> None:
    """Cleaning up a snapshot that doesn't exist succeeds without calling btrfs."""
    result = _run_helper_once(
        tmp_path,
        {
            "request_id": "req1",
            "operation": "cleanup",
            "target": "2026-06-12T03:43:57.123456Z",
        },
    )
    assert result["exit_code"] == 0
    assert not (tmp_path / "btrfs.log").exists()


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="bash and jq required",
)
def test_snapshot_helper_cleanup_existing_snapshot_deletes_by_name(
    tmp_path: Path,
) -> None:
    """Cleaning up an existing snapshot deletes exactly that named path."""
    name = "2026-06-12T03:43:57.123456Z"
    snapshot_path = tmp_path / "mngr-btrfs" / "snapshots" / name
    snapshot_path.mkdir(parents=True)
    result = _run_helper_once(tmp_path, {"request_id": "req1", "operation": "cleanup", "target": name})
    assert result["exit_code"] == 0
    btrfs_calls = (tmp_path / "btrfs.log").read_text()
    assert f"subvolume delete {snapshot_path}" in btrfs_calls
