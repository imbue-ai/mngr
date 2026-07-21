"""Pytest conftest for the mngr_vultr inner package.

Adds a session-end safety net that finds and destroys any Vultr VPS
instances tagged with this session's marker. Catches leaks from tests
that crashed between create and their ``try``/``finally`` destroy, or
whose destroy itself failed silently.

The mechanism is tag-based:
  1. ``pytest_configure`` injects two tags into ``MNGR_VPS_EXTRA_TAGS``
     via ``pytest.MonkeyPatch`` so the value is restored at session end
     (no process-wide leakage): ``mngr-vultr-test-session=<uuid>`` (the
     per-session marker used by the in-process leak check below) and
     ``mngr-vultr-test-created=<YYYY-MM-DD-HH-MM-SS>`` (a UTC session-start
     timestamp). ``build_vps_tags`` in mngr_vps reads this env var
     and attaches every entry to each VPS at create time, so every
     test-created VPS carries both tags. The mngr CLI runs as a
     subprocess and inherits the env, so this works transparently across
     ``_run_mngr`` calls.

     The timestamp tag feeds the out-of-band, age-based reaper in
     ``imbue.mngr_vultr.cleanup``: a session killed mid-run leaves orphans
     that no future session -- each with a fresh uuid -- can match, so the
     reaper finds them by age instead. The tag is built by
     ``build_test_created_tag`` so its format stays in lockstep with the
     reaper that parses it.
  2. ``pytest_sessionfinish`` lists Vultr instances, filters to those
     bearing the session tag, destroys any survivors, and fails the
     session on any real leak (see ``_is_real_leak``).

Gated on the ``MNGR_VULTR_RELEASE_TESTS=1`` opt-in (the same flag that
gates the release tests in ``test_release_vultr.py``): when it is unset
this is an ordinary unit-only run that creates no Vultr instances, so the
hooks no-op. When the opt-in *is* set but ``VULTR_API_KEY`` is missing,
``pytest_sessionfinish`` fails the session rather than skipping silently --
a release run with no key could not have scanned for leaks.
"""

import os
import uuid
from datetime import datetime
from datetime import timezone
from enum import auto
from typing import Any
from typing import Final
from typing import assert_never

import pytest
from loguru import logger
from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vultr.cleanup import build_test_created_tag
from imbue.mngr_vultr.client import VultrVpsClient
from imbue.mngr_vultr.testing import VULTR_RELEASE_TESTS_OPT_IN
from imbue.mngr_vultr.testing import VULTR_TEST_OS_ID

_SESSION_TAG_KEY: Final[str] = "mngr-vultr-test-session"
_SESSION_TAG: Final[str] = f"{_SESSION_TAG_KEY}={uuid.uuid4().hex}"
_CREATED_TAG: Final[str] = build_test_created_tag(datetime.now(timezone.utc))


class _LeakDestroyOutcome(UpperCaseStrEnum):
    """Outcome of attempting to destroy one survivor at session end."""

    DESTROYED = auto()
    ALREADY_GONE = auto()
    DESTROY_FAILED = auto()


# Used to set + restore MNGR_VPS_EXTRA_TAGS across the pytest session.
# Instantiated module-side so ``pytest_configure`` (setup) and
# ``pytest_sessionfinish`` (teardown via ``.undo()``) share state without
# a fixture, which session hooks cannot consume directly.
_monkeypatch: Final[pytest.MonkeyPatch] = pytest.MonkeyPatch()


def _mark_session_failed(session: pytest.Session) -> None:
    """Fail the session, but only if it was otherwise passing.

    Raising from ``pytest_sessionfinish`` is silently dropped by pytest, so
    setting ``session.exitstatus`` is the supported way to signal failure.
    """
    if session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_configure(config: pytest.Config) -> None:
    """Inject the session and timestamp tags into ``MNGR_VPS_EXTRA_TAGS``.

    No-ops when ``MNGR_VULTR_RELEASE_TESTS`` is unset. Uses
    ``pytest.MonkeyPatch.setenv`` so the original value is restored when
    ``_monkeypatch.undo()`` is called from ``pytest_sessionfinish``.
    """
    del config
    if not VULTR_RELEASE_TESTS_OPT_IN:
        return
    existing = os.environ.get("MNGR_VPS_EXTRA_TAGS", "").strip()
    session_tags = f"{_SESSION_TAG},{_CREATED_TAG}"
    new_value = f"{existing},{session_tags}" if existing else session_tags
    _monkeypatch.setenv("MNGR_VPS_EXTRA_TAGS", new_value)


def _list_leaked_instances(client: VultrVpsClient) -> list[dict[str, Any]]:
    """Return Vultr instances bearing this session's tag (i.e., leaked).

    A ``list_instances`` failure propagates: the caller fails the session rather
    than silently reporting "no leaks" for a scan that never ran.
    """
    instances = client.list_instances()
    return [inst for inst in instances if _SESSION_TAG in inst.get("tags", [])]


def _destroy_leaked_instance(client: VultrVpsClient, instance: dict[str, Any]) -> _LeakDestroyOutcome:
    """Best-effort destroy of one leaked instance; return the outcome."""
    instance_id = instance.get("id", "")
    try:
        client.destroy_instance(VpsInstanceId(instance_id))
        return _LeakDestroyOutcome.DESTROYED
    except VpsApiError as e:
        if e.status_code == 404:
            logger.debug("Leaked Vultr instance {} already gone (404)", instance_id)
            return _LeakDestroyOutcome.ALREADY_GONE
        logger.error("Failed to destroy leaked Vultr instance {}: {}", instance_id, e)
        return _LeakDestroyOutcome.DESTROY_FAILED


def _is_real_leak(outcome: _LeakDestroyOutcome) -> bool:
    """Whether the outcome is a real leak (should fail the session).

    Only ``ALREADY_GONE`` -- a 404 race with the test's own finally-block
    destroy -- is benign.
    """
    match outcome:
        case _LeakDestroyOutcome.DESTROYED | _LeakDestroyOutcome.DESTROY_FAILED:
            return True
        case _LeakDestroyOutcome.ALREADY_GONE:
            return False
        case _ as unreachable:
            assert_never(unreachable)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Find and destroy any Vultr instances leaked by this test session.

    No-ops when ``MNGR_VULTR_RELEASE_TESTS`` is unset. When the opt-in is set
    but ``VULTR_API_KEY`` is missing, or the scan fails, the session is failed.
    On finding a real leak, destroys each survivor and fails the session.
    Restores ``MNGR_VPS_EXTRA_TAGS`` via ``_monkeypatch.undo()``.
    """
    del exitstatus
    try:
        if not VULTR_RELEASE_TESTS_OPT_IN:
            return
        api_key = os.environ.get("VULTR_API_KEY", "")
        if not api_key:
            logger.error(
                "MNGR_VULTR_RELEASE_TESTS=1 is set but VULTR_API_KEY is missing, so the "
                "session-end leak scan cannot run. Export VULTR_API_KEY, or unset "
                "MNGR_VULTR_RELEASE_TESTS to skip the Vultr release tests."
            )
            _mark_session_failed(session)
            return
        # os_id is required by the VultrVpsClient constructor but only used by
        # create_instance, which we never call from this cleanup path.
        client = VultrVpsClient(api_key=SecretStr(api_key), os_id=VULTR_TEST_OS_ID)
        try:
            leaked = _list_leaked_instances(client)
        except VpsApiError as e:
            logger.error("Failed to scan for leaked Vultr test instances: {}", e)
            _mark_session_failed(session)
            return
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
        outcomes = [_destroy_leaked_instance(client, inst) for inst in leaked]
        real_leak_count = sum(1 for outcome in outcomes if _is_real_leak(outcome))
        if real_leak_count > 0:
            _mark_session_failed(session)
    finally:
        _monkeypatch.undo()
