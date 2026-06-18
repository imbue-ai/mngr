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
     timestamp). ``build_vps_tags`` in mngr_vps_docker reads this env var
     and attaches every entry to each VPS at create time, so every
     test-created VPS carries both tags. The mngr CLI runs as a
     subprocess and inherits the env, so this works transparently across
     ``_run_mngr`` calls.

     The timestamp tag feeds the *out-of-band, age-based* reaper in
     ``imbue.mngr_vultr.cleanup`` (driven by
     ``scripts/cleanup_old_vultr_test_instances.py``, analogous to Modal's
     ``cleanup_old_modal_test_environments.py``): the in-process check in
     ``pytest_sessionfinish`` only reaps leaks from sessions that survive
     to run it, so a session/runner killed mid-run leaves orphans that no
     future session -- each with a fresh uuid -- can match. The timestamp
     lets the out-of-band reaper find and destroy those by age, independent
     of the random session uuid. The tag is built by
     ``build_test_created_tag`` so its format stays in lockstep with the
     reaper that parses it.
  2. ``pytest_sessionfinish`` lists Vultr instances, filters to those
     bearing the session tag, destroys any survivors, and fails the
     session on any real leak. A real leak is any survivor that was
     still alive at list time -- either we destroyed it
     (``DESTROYED``) or we could not destroy it (``DESTROY_FAILED``,
     worse: the instance stays live). Only the 404-race-with-test-
     finally case (``ALREADY_GONE``) is benign and does not fail.

Gated on the ``MNGR_VULTR_RELEASE_TESTS=1`` opt-in (the same flag that
gates the release tests in ``test_release_vultr.py``): when it is unset
this is an ordinary unit-only run that creates no Vultr instances, so the
hooks no-op. When the opt-in *is* set but ``VULTR_API_KEY`` is missing,
``pytest_sessionfinish`` fails the session rather than skipping silently --
a release run with no key is a misconfiguration, not a benign skip.
Modeled on the leak detector in
``libs/mngr_modal/imbue/mngr_modal/conftest.py`` and the opt-in gating in
the mngr_aws / mngr_gcp / mngr_azure conftests.
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
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vultr.cleanup import build_test_created_tag
from imbue.mngr_vultr.client import VultrVpsClient
from imbue.mngr_vultr.testing import VULTR_RELEASE_TESTS_OPT_IN
from imbue.mngr_vultr.testing import VULTR_TEST_OS_ID

_SESSION_TAG_KEY: Final[str] = "mngr-vultr-test-session"
_SESSION_TAG: Final[str] = f"{_SESSION_TAG_KEY}={uuid.uuid4().hex}"

# Timestamp tag for out-of-band age-based reaping (of sessions killed before
# ``pytest_sessionfinish`` runs). Built by ``build_test_created_tag`` so the
# format stays in lockstep with the reaper that parses it
# (``imbue.mngr_vultr.cleanup``). Computed at import (session start), which is
# within seconds of the first VPS create.
_CREATED_TAG: Final[str] = build_test_created_tag(datetime.now(timezone.utc))


class _LeakDestroyOutcome(UpperCaseStrEnum):
    """Outcome of attempting to destroy one survivor at session end.

    DESTROYED -- destroy API call succeeded on a still-running instance.
    ALREADY_GONE -- destroy returned 404; the instance disappeared between
        ``list_instances`` and ``destroy_instance``. This is the
        race-with-test-finally-block-destroy case and must not fail the
        session.
    DESTROY_FAILED -- any other failure (5xx, 429, 422, malformed dict).
        Counted as a real leak because we found a tagged survivor AND
        could not clean it up.
    """

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
    Only overwrite a successful (0) status: a non-zero status
    (INTERRUPTED=2, INTERNAL_ERROR=3, USAGE_ERROR=4, NO_TESTS_COLLECTED=5)
    carries strictly more diagnostic information than TESTS_FAILED=1, so
    downgrading would hide the real reason CI failed.
    """
    if session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_configure(config: pytest.Config) -> None:
    """Inject the session and timestamp tags into ``MNGR_VPS_EXTRA_TAGS``.

    Any VPS subsequently created via the mngr CLI (which runs as a
    subprocess and inherits this env) will carry both tags: the session
    tag makes the leak detection in ``pytest_sessionfinish`` precise, and
    the timestamp tag lets an out-of-band reaper destroy orphans by age
    when a session dies before ``pytest_sessionfinish`` can run. Uses
    ``pytest.MonkeyPatch.setenv`` so the original value is restored
    when ``_monkeypatch.undo()`` is called from ``pytest_sessionfinish``.

    No-ops when ``MNGR_VULTR_RELEASE_TESTS`` is unset: without the opt-in no
    release test runs, so nothing creates a VPS and there are no tags to
    inject. ``pytest_sessionfinish`` still calls ``_monkeypatch.undo()``
    unconditionally, which is a harmless no-op when nothing was set here.
    """
    del config
    if not VULTR_RELEASE_TESTS_OPT_IN:
        return
    existing = os.environ.get("MNGR_VPS_EXTRA_TAGS", "").strip()
    session_tags = f"{_SESSION_TAG},{_CREATED_TAG}"
    new_value = f"{existing},{session_tags}" if existing else session_tags
    _monkeypatch.setenv("MNGR_VPS_EXTRA_TAGS", new_value)


def _list_leaked_instances(client: VultrVpsClient) -> list[dict[str, Any]]:
    """Return Vultr instances bearing this session's tag (i.e., leaked)."""
    try:
        instances = client.list_instances()
    except VpsApiError as e:
        logger.warning("Vultr session-end leak check: list_instances failed: {}", e)
        return []
    return [inst for inst in instances if _SESSION_TAG in inst.get("tags", [])]


def _destroy_leaked_instance(client: VultrVpsClient, instance: dict[str, Any]) -> _LeakDestroyOutcome:
    """Best-effort destroy of one leaked instance.

    Returns the outcome as a ``_LeakDestroyOutcome``:

    * ``DESTROYED`` -- destroy succeeded on a still-running instance.
    * ``ALREADY_GONE`` -- destroy returned 404; the instance disappeared
      between our list and our destroy. This is the race with the
      test's own ``finally``-block ``mngr destroy --force`` and must
      not fail the session (else every successful release run would
      false-positive).
    * ``DESTROY_FAILED`` -- any other failure. Logged at error level;
      the session-end caller counts this as a real leak because we
      have a confirmed tagged survivor that we could not clean up.

    Does not raise: the goal is to attempt cleanup of every survivor
    before the session exits, then surface the outcome to the caller.
    """
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
    """Classify a destroy outcome as a real leak (should fail the session).

    ``DESTROYED`` and ``DESTROY_FAILED`` are real leaks: in both cases a
    survivor was alive at list time and either we destroyed it (test
    bug: it should have been gone already) or we could not destroy it
    (also a bug, with the additional badness that the instance stays
    live). ``ALREADY_GONE`` is the benign race with the test's own
    ``finally``-block destroy.
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

    Implemented as a hook (not a fixture) so it runs after every fixture
    teardown -- mirrors the Modal session-end leak check in
    ``libs/mngr_modal/imbue/mngr_modal/conftest.py``. No-ops when
    ``MNGR_VULTR_RELEASE_TESTS`` is unset (an ordinary unit-only run that
    created no instances -> nothing to scan). When the opt-in *is* set but
    ``VULTR_API_KEY`` is missing, the session is failed rather than skipped:
    a release run with no key cannot have created or scanned for instances,
    which is a misconfiguration worth surfacing loudly, not a benign skip.

    On finding a real leak -- a destroy that either succeeded on a
    still-running instance (``DESTROYED``) or itself failed
    (``DESTROY_FAILED``, meaning the survivor stays live) -- logs a
    loud error, destroys each survivor, and sets ``session.exitstatus``
    to ``TESTS_FAILED`` so CI surfaces the bug. The ``ALREADY_GONE``
    case (404 race with the test's own ``finally``-block destroy) is
    benign and does not fail the session. Only overwrites a successful
    exit status -- preserves the more-specific non-zero codes
    (INTERRUPTED, INTERNAL_ERROR, USAGE_ERROR, NO_TESTS_COLLECTED),
    which carry strictly more diagnostic information than TESTS_FAILED.

    Restores ``MNGR_VPS_EXTRA_TAGS`` to its pre-session value via
    ``_monkeypatch.undo()`` so the env mutation does not outlive the
    pytest run.
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
        outcomes = [_destroy_leaked_instance(client, inst) for inst in leaked]
        real_leak_count = sum(1 for outcome in outcomes if _is_real_leak(outcome))
        if real_leak_count > 0:
            _mark_session_failed(session)
    finally:
        _monkeypatch.undo()
