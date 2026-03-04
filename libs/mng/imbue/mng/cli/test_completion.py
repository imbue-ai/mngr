import json
import os
import time
from pathlib import Path

import psutil
import pytest

from imbue.mng.cli.complete import _AGENT_COMPLETIONS_CACHE_FILENAME
from imbue.mng.cli.complete import _BACKGROUND_REFRESH_COOLDOWN_SECONDS
from imbue.mng.cli.complete import _trigger_background_refresh
from imbue.mng.utils.polling import wait_for


def _write_cache(cache_dir: Path, names: list[str]) -> Path:
    """Write an agent completions cache file with the given names."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / _AGENT_COMPLETIONS_CACHE_FILENAME
    data = {"names": names, "updated_at": "2025-01-01T00:00:00+00:00"}
    cache_path.write_text(json.dumps(data))
    return cache_path


def _is_completion_refresh_process(proc: psutil.Process) -> bool:
    """Check if a process is a background completion refresh subprocess."""
    try:
        cmdline = " ".join(proc.cmdline())
        return "imbue.mng.main" in cmdline and "list" in cmdline
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


@pytest.mark.timeout(30)
def test_trigger_background_refresh_throttles_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disable_remote_providers_for_subprocesses: Path,
) -> None:
    """Stale cache triggers a refresh that updates the file; fresh cache does not."""
    # Point the completion cache at a temp directory we control.
    cache_dir = tmp_path / "completions"
    cache_dir.mkdir()
    monkeypatch.setenv("MNG_COMPLETION_CACHE_DIR", str(cache_dir))

    cache_path = cache_dir / _AGENT_COMPLETIONS_CACHE_FILENAME

    # -- Stale cache: the spawned subprocess should rewrite the cache file --
    _write_cache(cache_dir, ["agent"])
    old_time = time.time() - _BACKGROUND_REFRESH_COOLDOWN_SECONDS - 10
    os.utime(cache_path, (old_time, old_time))
    # Read back the mtime rather than using old_time directly, since some
    # filesystems round or truncate timestamps.
    stale_mtime = cache_path.stat().st_mtime

    _trigger_background_refresh()

    wait_for(
        lambda: cache_path.stat().st_mtime != stale_mtime,
        timeout=15.0,
        error_message="Stale cache should trigger a background refresh that updates the file",
    )

    # Wait for the stale-cache process to exit before testing the fresh-cache path.
    def _no_refresh_children() -> bool:
        return not any(_is_completion_refresh_process(c) for c in psutil.Process().children(recursive=True))

    wait_for(_no_refresh_children, timeout=10.0, error_message="Background refresh process did not exit")

    # -- Fresh cache: calling again immediately should be throttled --
    # _trigger_background_refresh is synchronous up to the Popen call,
    # so if throttling fails, the child process exists immediately after return.
    children_before = set(p.pid for p in psutil.Process().children(recursive=True))

    _trigger_background_refresh()

    children_after = set(p.pid for p in psutil.Process().children(recursive=True))
    new_children = children_after - children_before
    assert new_children == set(), "Fresh cache should prevent spawning"
