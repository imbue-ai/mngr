"""Pytest conftest for the mngr_vultr inner package.

Adds a session-end safety net that finds and destroys any Vultr VPS
instances tagged with this session's marker. Catches leaks from tests
that crashed between create and their ``try``/``finally`` destroy, or
whose destroy itself failed silently.

The mechanism is tag-based:
  1. ``pytest_configure`` injects ``mngr-vultr-test-session=<uuid>`` into
     ``MNGR_VPS_EXTRA_TAGS``. ``build_vps_tags`` in mngr_vps_docker
     reads this env var and attaches every entry to each VPS at
     create time, so every test-created VPS carries the session tag.
     The mngr CLI runs as a subprocess and inherits the env, so this
     works transparently across ``_run_mngr`` calls.
  2. ``pytest_sessionfinish`` lists Vultr instances, filters to those
     bearing the session tag, destroys any survivors, and fails the
     session if any leaks were found.

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


def pytest_configure(config: pytest.Config) -> None:
    """Inject the session tag into ``MNGR_VPS_EXTRA_TAGS``.

    Any VPS subsequently created via the mngr CLI (which runs as a
    subprocess and inherits this env) will carry the tag, making the
    leak detection in ``pytest_sessionfinish`` precise.
    """
    del config
    existing = os.environ.get("MNGR_VPS_EXTRA_TAGS", "").strip()
    os.environ["MNGR_VPS_EXTRA_TAGS"] = f"{existing},{_SESSION_TAG}" if existing else _SESSION_TAG


def _list_leaked_instances(client: VultrVpsClient) -> list[dict[str, Any]]:
    """Return Vultr instances bearing this session's tag (i.e., leaked)."""
    try:
        instances = client.list_instances()
    except VpsApiError as e:
        logger.warning("Vultr session-end leak check: list_instances failed: {}", e)
        return []
    return [inst for inst in instances if _SESSION_TAG in inst.get("tags", [])]


def _destroy_leaked_instance(client: VultrVpsClient, instance: dict[str, Any]) -> None:
    """Best-effort destroy of one leaked instance.

    Logs but does not raise on failure: the goal is to clean up as many
    survivors as possible before the session exits. ``404`` is treated
    as benign (the instance disappeared between list and destroy --
    likely an in-flight destroy from the test's ``finally`` block).
    """
    instance_id = instance.get("id", "")
    try:
        client.destroy_instance(VpsInstanceId(instance_id))
    except VpsApiError as e:
        if e.status_code == 404:
            logger.debug("Leaked Vultr instance {} already gone (404)", instance_id)
            return
        logger.error("Failed to destroy leaked Vultr instance {}: {}", instance_id, e)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Find and destroy any Vultr instances leaked by this test session.

    Implemented as a hook (not a fixture) so it runs after every fixture
    teardown -- mirrors the Modal session-end leak check in
    ``libs/mngr_modal/imbue/mngr_modal/conftest.py``. Skipped silently
    when ``VULTR_API_KEY`` is unset (no API key -> no instances were
    ever created -> nothing to scan).

    On finding leaks: logs a loud error, destroys each survivor, and
    sets ``session.exitstatus`` to ``TESTS_FAILED`` so CI surfaces the
    bug rather than silently absorbing the cleanup. Only overwrites a
    successful exit status -- preserves the more-specific non-zero
    codes (INTERRUPTED, INTERNAL_ERROR, USAGE_ERROR, NO_TESTS_COLLECTED),
    which carry strictly more diagnostic information than TESTS_FAILED.
    """
    del exitstatus
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
    for inst in leaked:
        _destroy_leaked_instance(client, inst)
    if session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
