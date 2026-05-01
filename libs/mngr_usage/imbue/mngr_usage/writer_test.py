"""Integration tests for the claude_rate_limits_writer.sh writer.

We exercise the bash writer directly via subprocess to ensure both modes
(statusline / sdk) merge into the cache correctly with last-write-wins
per-window semantics.
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


def _run_writer(writer_path: Path, mode: str, stdin: str, cache_path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "MNGR_RATE_LIMITS_CACHE": str(cache_path)}
    return subprocess.run(
        [str(writer_path), mode],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def test_writer_statusline_creates_cache(writer_path: Path, cache_path: Path) -> None:
    payload = json.dumps(
        {
            "rate_limits": {
                "five_hour": {"used_percentage": 73.4, "resets_at": 1777673400},
                "seven_day": {"used_percentage": 41.0, "resets_at": 1778000000},
            }
        }
    )
    result = _run_writer(writer_path, "statusline", payload, cache_path)
    assert result.returncode == 0, result.stderr

    data = json.loads(cache_path.read_text())
    assert data["schema_version"] == 1
    assert data["windows"]["five_hour"]["used_percentage"] == 73.4
    assert data["windows"]["five_hour"]["resets_at"] == 1777673400
    assert data["windows"]["five_hour"]["source"] == "statusline"
    assert data["windows"]["seven_day"]["used_percentage"] == 41.0


def test_writer_sdk_merges_per_window_last_write_wins(writer_path: Path, cache_path: Path) -> None:
    statusline_payload = json.dumps(
        {
            "rate_limits": {
                "five_hour": {"used_percentage": 50.0, "resets_at": 1700000000},
            }
        }
    )
    r1 = _run_writer(writer_path, "statusline", statusline_payload, cache_path)
    assert r1.returncode == 0, r1.stderr

    sdk_payload = "\n".join(
        [
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "rateLimitType": "5h",
                        "status": "rejected",
                        "resetsAt": 1700001234,
                        "isUsingOverage": True,
                    },
                }
            )
        ]
    )
    r2 = _run_writer(writer_path, "sdk", sdk_payload, cache_path)
    assert r2.returncode == 0, r2.stderr

    data = json.loads(cache_path.read_text())
    five_hour = data["windows"]["five_hour"]
    # Statusline-set fields are preserved (per-window last-write-wins is
    # actually per-field merge in the spec; missing fields stay nulled but
    # already-set ones are not destroyed unless the new writer overwrites them).
    assert five_hour["used_percentage"] == 50.0
    # SDK-set fields are present
    assert five_hour["status"] == "rejected"
    assert five_hour["resets_at"] == 1700001234
    assert five_hour["is_using_overage"] is True
    assert five_hour["source"] == "sdk"


def test_writer_handles_missing_fields_gracefully(writer_path: Path, cache_path: Path) -> None:
    payload = json.dumps({"rate_limits": {"five_hour": {}}})
    result = _run_writer(writer_path, "statusline", payload, cache_path)
    assert result.returncode == 0, result.stderr
    data = json.loads(cache_path.read_text())
    assert data["windows"]["five_hour"]["used_percentage"] is None
    assert data["windows"]["five_hour"]["resets_at"] is None
    assert data["windows"]["five_hour"]["source"] == "statusline"


def test_writer_ignores_unknown_event_types(writer_path: Path, cache_path: Path) -> None:
    payload = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "rate_limit_event", "rate_limit_info": {"rateLimitType": "unknown"}}),
        ]
    )
    result = _run_writer(writer_path, "sdk", payload, cache_path)
    assert result.returncode == 0, result.stderr
    # No file should be created since no known windows were updated, but writer
    # always writes a cache file (even if empty windows). Tolerate either.
    if cache_path.exists():
        data = json.loads(cache_path.read_text())
        assert data.get("windows", {}) == {} or "unknown" not in data["windows"]


def test_writer_unknown_mode_errors(writer_path: Path, cache_path: Path) -> None:
    result = _run_writer(writer_path, "bogus", "{}", cache_path)
    assert result.returncode == 64
    assert "unknown mode" in result.stderr or "expected" in result.stderr


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
        futures = [pool.submit(_run_writer, writer_path, "statusline", payload, cache_path) for payload in payloads]
        for f in futures:
            r = f.result()
            assert r.returncode == 0, r.stderr
    data = json.loads(cache_path.read_text())
    assert data["schema_version"] == 1
    assert "five_hour" in data["windows"]
