"""Pytest conftest for the mngr_vultr inner package.

Adds a session-end safety net that finds and destroys any Vultr VPS
instances tagged with this session's marker. Catches leaks from tests
that crashed between create and their ``try``/``finally`` destroy, or
whose destroy itself failed silently.

The mechanism is tag-based:
  1. ``pytest_configure`` injects ``mngr-vultr-test-session=<uuid>`` into
     ``MNGR_VPS_EXTRA_TAGS`` via ``pytest.MonkeyPatch`` so the value is
     restored at session end (no process-wide leakage). ``build_vps_tags``
     in mngr_vps_docker reads this env var and attaches every entry to
     each VPS at create time, so every test-created VPS carries the
     session tag. The mngr CLI runs as a subprocess and inherits the
     env, so this works transparently across ``_run_mngr`` calls.
  2. ``pytest_sessionfinish`` lists Vultr instances, filters to those
     bearing the session tag, destroys any survivors, and -- when at
     least one destroy actually deleted a still-running instance --
     fails the session.

Skipped silently when ``VULTR_API_KEY`` is unset (unit-only runs make
no Vultr API calls). Modeled on the leak detector in
``libs/mngr_modal/imbue/mngr_modal/conftest.py``.
"""

import os
import uuid
from typing import Any
from typing import Final

import pytest
from loguru import logger
from pydantic import SecretStr

from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vultr.client import VultrVpsClient

_SESSION_TAG_KEY: Final[str] = "mngr-vultr-test-session"
_SESSION_TAG: Final[str] = f"{_SESSION_TAG_KEY}={uuid.uuid4().hex}"

# Used to set + restore MNGR_VPS_EXTRA_TAGS across the pytest session.
# Instantiated module-side so ``pytest_configure`` (setup) and
# ``pytest_sessionfinish`` (teardown via ``.undo()``) share state without
# a fixture, which session hooks cannot consume directly.
_monkeypatch: Final[pytest.MonkeyPatch] = pytest.MonkeyPatch()


def pytest_configure(config: pytest.Config) -> None:
    """Inject the session tag into ``MNGR_VPS_EXTRA_TAGS``.

    Any VPS subsequently created via the mngr CLI (which runs as a
    subprocess and inherits this env) will carry the tag, making the
    leak detection in ``pytest_sessionfinish`` precise. Uses
    ``pytest.MonkeyPatch.setenv`` so the original value is restored
    when ``_monkeypatch.undo()`` is called from ``pytest_sessionfinish``.
    """
    del config
    existing = os.environ.get("MNGR_VPS_EXTRA_TAGS", "").strip()
    new_value = f"{existing},{_SESSION_TAG}" if existing else _SESSION_TAG
    _monkeypatch.setenv("MNGR_VPS_EXTRA_TAGS", new_value)


def _list_leaked_instances(client: VultrVpsClient) -> list[dict[str, Any]]:
    """Return Vultr instances bearing this session's tag (i.e., leaked)."""
    try:
        instances = client.list_instances()
    except VpsApiError as e:
        logger.warning("Vultr session-end leak check: list_instances failed: {}", e)
        return []
    return [inst for inst in instances if _SESSION_TAG in inst.get("tags", [])]


def _destroy_leaked_instance(client: VultrVpsClient, instance: dict[str, Any]) -> bool:
    """Best-effort destroy of one leaked instance.

    Returns ``True`` only when the destroy actually deleted a
    still-running instance. ``404`` is treated as benign and returns
    ``False`` -- the instance disappeared between our list and our
    destroy, which is what happens when a test's own ``finally``-block
    ``mngr destroy --force`` was still in-flight at list time. Counting
    those as leaks would false-positive every successful release run.
    Logs but does not raise on other failures: the goal is to clean up
    as many survivors as possible before the session exits.
    """
    instance_id = instance.get("id", "")
    try:
        client.destroy_instance(VpsInstanceId(instance_id))
        return True
    except VpsApiError as e:
        if e.status_code == 404:
            logger.debug("Leaked Vultr instance {} already gone (404)", instance_id)
            return False
        logger.error("Failed to destroy leaked Vultr instance {}: {}", instance_id, e)
        return False


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Find and destroy any Vultr instances leaked by this test session.

    Implemented as a hook (not a fixture) so it runs after every fixture
    teardown -- mirrors the Modal session-end leak check in
    ``libs/mngr_modal/imbue/mngr_modal/conftest.py``. Skipped silently
    when ``VULTR_API_KEY`` is unset (no API key -> no instances were
    ever created -> nothing to scan).

    On finding a real leak (destroy actually deleted a still-running
    instance, not a race with the test's own ``finally``): logs a loud
    error, destroys each survivor, and sets ``session.exitstatus`` to
    ``TESTS_FAILED`` so CI surfaces the bug. Only overwrites a
    successful exit status -- preserves the more-specific non-zero
    codes (INTERRUPTED, INTERNAL_ERROR, USAGE_ERROR, NO_TESTS_COLLECTED),
    which carry strictly more diagnostic information than TESTS_FAILED.

    Restores ``MNGR_VPS_EXTRA_TAGS`` to its pre-session value via
    ``_monkeypatch.undo()`` so the env mutation does not outlive the
    pytest run.
    """
    del exitstatus
    try:
        api_key = os.environ.get("VULTR_API_KEY", "")
        if not api_key:
            return
        client = VultrVpsClient(api_key=SecretStr(api_key))
        leaked = _list_leaked_instances(client)
        if not leaked:
            return
        lines = "\n".join(f"  {inst.get('id', '')} (label={inst.get('label', '')})" for inst in leaked)
        logger.error(
            "{bar}\nVULTR SESSION CLEANUP FOUND LEAKED INSTANCES!\n{bar}\n\n"
            "Tests should destroy their Vultr VPSes before completing.\n"
            "Session tag: {tag}\n{lines}\n\nAttempting cleanup now...",
            bar="=" * 70,
            tag=_SESSION_TAG,
            lines=lines,
        )
        real_leak_count = sum(_destroy_leaked_instance(client, inst) for inst in leaked)
        if real_leak_count > 0 and session.exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
    finally:
        _monkeypatch.undo()
