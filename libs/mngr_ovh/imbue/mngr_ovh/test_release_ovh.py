"""End-to-end release tests for the OVH provider.

These tests create and destroy real OVH VPS instances and require OVH
credentials (any of OAuth2 OR application key + secret + consumer key,
or a populated ``~/.ovh.conf``).

They are marked with @pytest.mark.release so they only run in CI or
when explicitly requested via ``just test <path>::<test>``.

Because OVH bills monthly, ``destroy_host`` forfeits the prorated
remainder of the month -- so we keep this test suite small and serialize
it via a per-test name suffix to make collisions visible if anything is
left dangling.
"""

import os
import subprocess
import time
from uuid import uuid4

import ovh
import pytest

from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_ovh.config import OvhProviderConfig


def _has_ovh_credentials() -> bool:
    """True iff env vars or ``~/.ovh.conf`` provide enough to authenticate."""
    config = OvhProviderConfig()
    if config.has_explicit_credentials():
        return True
    home_conf = os.path.expanduser("~/.ovh.conf")
    return os.path.isfile(home_conf)


_HAS_OVH = _has_ovh_credentials()

pytestmark = [
    pytest.mark.release,
    pytest.mark.timeout(1800),
    pytest.mark.skipif(not _HAS_OVH, reason="OVH credentials not configured"),
]


def _run_mngr(*args: str, timeout: int = 1200) -> subprocess.CompletedProcess[str]:
    cmd = ["uv", "run", "mngr", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=os.environ.get("MNGR_REPO_ROOT", os.getcwd()),
    )


def _build_client() -> OvhVpsClient:
    config = OvhProviderConfig()
    raw = ovh.Client(**config.resolve_python_ovh_kwargs())
    return OvhVpsClient(ovh_client=raw, subsidiary=config.ovh_subsidiary)


def _destroy_agent(agent_name: str) -> None:
    """Best-effort ``mngr destroy`` for a release-test VPS, then settle."""
    subprocess.run(
        ["uv", "run", "mngr", "destroy", agent_name, "--force"],
        input="y\n",
        capture_output=True,
        text=True,
        timeout=300,
        cwd=os.environ.get("MNGR_REPO_ROOT", os.getcwd()),
    )
    time.sleep(30)


def test_ovh_lifecycle_create_exec_and_destroy() -> None:
    """End-to-end create/exec/destroy through the mngr CLI."""
    # ``uuid4().hex`` (not ``time.time() % N``) so two overlapping release
    # runs cannot collide on a name and clobber/leak a real billed VPS.
    agent_name = f"test-ovh-{uuid4().hex}"

    result = _run_mngr(
        "create",
        agent_name,
        "--provider",
        "ovh",
        "--no-connect",
        "--message",
        "just say hello",
    )
    assert result.returncode == 0, f"Create failed: {result.stderr}"

    try:
        result = _run_mngr("exec", agent_name, "echo hello-from-ovh")
        assert result.returncode == 0, f"Exec failed: {result.stderr}"
        assert "hello-from-ovh" in result.stdout

        result = _run_mngr("exec", agent_name, "test -d /mngr && echo exists")
        assert result.returncode == 0, f"host_dir check failed: {result.stderr}"
        assert "exists" in result.stdout

        result = _run_mngr("list")
        assert result.returncode == 0, f"List failed: {result.stderr}"
        assert agent_name in result.stdout
        assert "ovh" in result.stdout
    finally:
        _destroy_agent(agent_name)


def test_ovh_lifecycle_create_stop_start_destroy() -> None:
    """End-to-end create/stop/start/destroy through the mngr CLI."""
    agent_name = f"test-ovh-ss-{uuid4().hex}"

    result = _run_mngr(
        "create",
        agent_name,
        "--provider",
        "ovh",
        "--no-connect",
        "--message",
        "just say hello",
    )
    assert result.returncode == 0, f"Create failed: {result.stderr}"

    try:
        result = _run_mngr("stop", agent_name)
        assert result.returncode == 0, f"Stop failed: {result.stderr}"

        result = _run_mngr("list")
        assert result.returncode == 0
        assert agent_name in result.stdout

        result = _run_mngr("start", agent_name, "--no-connect")
        assert result.returncode == 0, f"Start failed: {result.stderr}"

        result = _run_mngr("exec", agent_name, "echo alive-after-restart")
        assert result.returncode == 0, f"Post-restart exec failed: {result.stderr}"
        assert "alive-after-restart" in result.stdout
    finally:
        _destroy_agent(agent_name)


def test_ovh_client_list_instances_does_not_error() -> None:
    """Read-only smoke test against the live OVH API: listing must round-trip."""
    client = _build_client()
    assert isinstance(client.list_instances(), list)
