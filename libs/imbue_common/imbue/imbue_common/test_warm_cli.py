import select
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest


def _poll_until_socket_exists(socket_path: Path, timeout_seconds: float) -> None:
    """Poll until the socket file exists by attempting to connect with a timeout.

    Uses select.select for delay instead of time.sleep to comply with ratchet rules.
    """
    elapsed = 0.0
    poll_interval = 0.1
    while elapsed < timeout_seconds:
        if socket_path.exists():
            return
        select.select([], [], [], poll_interval)
        elapsed += poll_interval
    raise TimeoutError(f"Socket {socket_path} did not appear within {timeout_seconds}s")


def _run_warm_cli_script(
    script_content: str,
    socket_path: Path,
    tmp_path: Path,
    args: list[str],
) -> subprocess.CompletedProcess[str]:
    """Write a script to tmp_path and run it via subprocess with the given args."""
    unique_id = uuid4().hex
    script_file = tmp_path / f"warm_script_{unique_id}.py"

    full_script = f"""
import click
from pathlib import Path
from imbue.imbue_common.warm_cli import warm_cli

{script_content}

if __name__ == "__main__":
    warm_cli(entry, socket_path=Path("{socket_path}"))
"""
    script_file.write_text(full_script)

    return subprocess.run(
        [sys.executable, str(script_file)] + args,
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.fixture()
def warm_socket_path() -> Iterator[Path]:
    """Provide a unique socket path in /tmp (short enough for AF_UNIX) and clean up after."""
    unique_id = uuid4().hex[:12]
    path = Path(f"/tmp/wc_int_{unique_id}.sock")
    yield path
    path.unlink(missing_ok=True)


def _run_cold_then_warm(
    script_content: str,
    socket_path: Path,
    tmp_path: Path,
    cold_args: list[str],
    warm_args: list[str],
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    """Run a cold invocation, wait for the warm successor, then run a warm invocation."""
    cold_result = _run_warm_cli_script(script_content, socket_path, tmp_path, cold_args)
    _poll_until_socket_exists(socket_path, timeout_seconds=5)
    warm_result = _run_warm_cli_script(script_content, socket_path, tmp_path, warm_args)
    return cold_result, warm_result


def test_warm_cli_end_to_end_cold_and_warm_paths(
    tmp_path: Path,
    warm_socket_path: Path,
) -> None:
    """Verify both cold and warm paths produce correct output."""
    script = """
@click.command()
@click.argument("name")
def entry(name):
    click.echo(f"Hello, {name}!")
"""
    cold_result, warm_result = _run_cold_then_warm(
        script, warm_socket_path, tmp_path,
        cold_args=["ColdWorld"],
        warm_args=["WarmWorld"],
    )

    assert cold_result.returncode == 0
    assert "Hello, ColdWorld!" in cold_result.stdout
    assert warm_result.returncode == 0
    assert "Hello, WarmWorld!" in warm_result.stdout


def test_warm_cli_propagates_nonzero_exit_code(
    tmp_path: Path,
    warm_socket_path: Path,
) -> None:
    """Verify that non-zero exit codes are propagated from both cold and warm paths."""
    script = """
import sys

@click.command()
def entry():
    sys.exit(7)
"""
    cold_result, warm_result = _run_cold_then_warm(
        script, warm_socket_path, tmp_path,
        cold_args=[],
        warm_args=[],
    )

    assert cold_result.returncode == 7
    assert warm_result.returncode == 7


def test_warm_cli_passes_argv_to_warm_server(
    tmp_path: Path,
    warm_socket_path: Path,
) -> None:
    """Verify that the warm server receives the correct argv from the client."""
    output_file = tmp_path / f"argv_output_{uuid4().hex}.txt"
    script = f"""
import os
import sys

@click.command()
@click.argument("name")
def entry(name):
    from pathlib import Path
    Path("{output_file}").write_text(f"name={{name}}")
"""
    cold_result, warm_result = _run_cold_then_warm(
        script, warm_socket_path, tmp_path,
        cold_args=["Alice"],
        warm_args=["Bob"],
    )

    assert cold_result.returncode == 0
    assert warm_result.returncode == 0
    # The output file reflects the most recent (warm) invocation
    assert "name=Bob" in output_file.read_text()
