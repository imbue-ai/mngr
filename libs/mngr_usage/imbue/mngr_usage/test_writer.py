"""Integration tests for the claude_rate_limits_writer.sh writer.

We exercise the bash writer directly via subprocess to ensure it merges
statusline payloads into the cache atomically and tolerates concurrent writes.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from imbue.mngr_usage import resources as _usage_resources

WRITER_SCRIPT_NAME = "claude_rate_limits_writer.sh"


@pytest.fixture
def writer_path(tmp_path: Path) -> Path:
    """Stage the writer script onto disk with execute bit, ready for subprocess."""
    src = importlib.resources.files(_usage_resources).joinpath(WRITER_SCRIPT_NAME)
    dst = tmp_path / WRITER_SCRIPT_NAME
    dst.write_bytes(src.read_bytes())
    dst.chmod(0o755)
    return dst


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "claude_rate_limits.json"


def _has_jq() -> bool:
    return shutil.which("jq") is not None


pytestmark = pytest.mark.skipif(not _has_jq(), reason="jq not installed; required by claude_rate_limits_writer.sh")


def _run_writer(writer_path: Path, stdin: str, cache_path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "MNGR_RATE_LIMITS_CACHE": str(cache_path)}
    return subprocess.run(
        [str(writer_path)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def test_writer_creates_cache(writer_path: Path, cache_path: Path) -> None:
    payload = json.dumps(
        {
            "rate_limits": {
                "five_hour": {"used_percentage": 73.4, "resets_at": 1777673400},
                "seven_day": {"used_percentage": 41.0, "resets_at": 1778000000},
            }
        }
    )
    result = _run_writer(writer_path, payload, cache_path)
    assert result.returncode == 0, result.stderr

    data = json.loads(cache_path.read_text())
    assert data["schema_version"] == 1
    assert data["windows"]["five_hour"]["used_percentage"] == 73.4
    assert data["windows"]["five_hour"]["resets_at"] == 1777673400
    assert data["windows"]["five_hour"]["source"] == "statusline"
    assert data["windows"]["seven_day"]["used_percentage"] == 41.0


def test_writer_preserves_unknown_fields(writer_path: Path, cache_path: Path) -> None:
    """The writer only updates statusline-known fields. Any other fields already
    present on a window (e.g. left over from older cache schemas, or written by a
    future writer) must survive the merge unchanged."""
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "windows": {
                    "five_hour": {
                        "used_percentage": 10.0,
                        "resets_at": 1,
                        "status": "rejected",
                        "is_using_overage": True,
                        "source": "sdk",
                        "updated_at": 100,
                    }
                },
            }
        )
    )

    payload = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 50.0, "resets_at": 1700000000}}})
    result = _run_writer(writer_path, payload, cache_path)
    assert result.returncode == 0, result.stderr

    five_hour = json.loads(cache_path.read_text())["windows"]["five_hour"]
    # Statusline-known fields are overwritten:
    assert five_hour["used_percentage"] == 50.0
    assert five_hour["resets_at"] == 1700000000
    assert five_hour["source"] == "statusline"
    # Unknown-to-statusline fields are preserved:
    assert five_hour["status"] == "rejected"
    assert five_hour["is_using_overage"] is True


def test_writer_handles_missing_fields_gracefully(writer_path: Path, cache_path: Path) -> None:
    payload = json.dumps({"rate_limits": {"five_hour": {}}})
    result = _run_writer(writer_path, payload, cache_path)
    assert result.returncode == 0, result.stderr
    data = json.loads(cache_path.read_text())
    assert data["windows"]["five_hour"]["used_percentage"] is None
    assert data["windows"]["five_hour"]["resets_at"] is None
    assert data["windows"]["five_hour"]["source"] == "statusline"


def test_writer_resets_corrupt_cache(writer_path: Path, cache_path: Path) -> None:
    """A corrupt cache file should be replaced rather than failing the writer."""
    cache_path.write_text("not json")
    payload = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 1.0, "resets_at": 1}}})
    result = _run_writer(writer_path, payload, cache_path)
    assert result.returncode == 0, result.stderr
    data = json.loads(cache_path.read_text())
    assert data["windows"]["five_hour"]["used_percentage"] == 1.0


def test_writer_handles_concurrent_writes(writer_path: Path, cache_path: Path) -> None:
    """Concurrent statusline writes must end with a parsable cache file.

    We don't assert specific values because last-write-wins makes the final
    state non-deterministic; we only assert no torn JSON.
    """
    payloads = [
        json.dumps({"rate_limits": {"five_hour": {"used_percentage": float(i), "resets_at": 1700000000 + i}}})
        for i in range(20)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_run_writer, writer_path, payload, cache_path) for payload in payloads]
        for f in futures:
            r = f.result()
            assert r.returncode == 0, r.stderr
    data = json.loads(cache_path.read_text())
    assert data["schema_version"] == 1
    assert "five_hour" in data["windows"]
