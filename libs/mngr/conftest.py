"""Project-level conftest for mngr.

When running tests from libs/mngr/, this conftest provides the common pytest hooks
that would otherwise come from the monorepo root conftest.py (which is not discovered
when pytest runs from a subdirectory).

When running from the monorepo root, the root conftest.py registers the hooks first,
and this file's register_conftest_hooks() call is a no-op (guarded by a module-level flag).

Resource guards are discovered automatically from the resource_guards entry
point group, so no manual registration is needed here.
"""

import re
from collections.abc import Generator
from typing import Any

import pytest
from loguru import logger

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mngr.register_guards_docker import register_docker_cli_guard
from imbue.mngr.register_guards_docker import register_docker_sdk_guard
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr.utils.testing import WARNINGS_ALLOWED_STACK
from imbue.resource_guards.resource_guards import register_resource_guard

suppress_warnings()

register_resource_guard("tmux")
register_resource_guard("modal")
register_resource_guard("rsync")
register_resource_guard("unison")
register_docker_cli_guard()
register_docker_sdk_guard()

register_marker(
    "allow_warnings(match=None): opt out of the autouse 'no unexpected loguru warnings' check; "
    "if match is given, only warnings matching the regex are allowed"
)

register_conftest_hooks(globals())


# Per-test buffer of WARNING-or-higher loguru records the autouse fixture is
# watching. Reset by each fixture invocation. xdist workers are separate
# processes so this module-level state is per-worker.
_unexpected_warnings: list[str] = []


def _unexpected_warning_sink(message: Any) -> None:
    """Loguru sink that records WARNING+ records when not opted out.

    The top frame of WARNINGS_ALLOWED_STACK governs: if it is None, any
    warning is allowed; if it is a regex, only warnings whose message
    matches are allowed.
    """
    # record["message"] is the bare message body (no traceback), which is
    # what allow_warnings() match patterns are written against.
    if WARNINGS_ALLOWED_STACK:
        pattern = WARNINGS_ALLOWED_STACK[-1]
        if pattern is None or pattern.search(message.record["message"]):
            return
    # str(message) follows the sink's format ("{message}") and additionally
    # appends an exception traceback whenever an exception is bound to the
    # record, so it can be a multi-line block. We display this full text in
    # the failure summary (matching is done against the bare body above).
    text = str(message).rstrip("\n")
    _unexpected_warnings.append(text)


# TEMPORARY: Markers whose tests are unconditionally opted out of the warning
# check. The bulk auto-opt-out is a pragmatic shortcut for landing the
# warning-detection fixture without first auditing every test under these
# markers; it is NOT a final design decision. Each test in this set should be
# examined individually -- many of them legitimately emit operational warnings
# (Docker daemon errors, paramiko reconnects, modal sandbox noise, tmux server
# lifecycle, etc.) and should be migrated to a tight
# `@pytest.mark.allow_warnings(match=...)`; tests that emit nothing should
# simply lose the auto-opt-out so the fixture catches future regressions.
# Once that audit is complete this set should shrink (ideally to empty) and
# this comment can go away.
_AUTO_ALLOW_WARNINGS_MARKERS: frozenset[str] = frozenset(
    {"docker", "docker_sdk", "tmux", "modal", "acceptance", "release"}
)


def _compile_allow_warnings_pattern(marker: pytest.Mark) -> re.Pattern[str] | None:
    """Validate an ``@pytest.mark.allow_warnings`` marker and return its compiled pattern.

    Returns None when no ``match=`` kwarg was given (allow any warning). Raises
    TypeError with a helpful message when the marker is misused.
    """
    if marker.args:
        raise TypeError(
            "@pytest.mark.allow_warnings takes only the 'match' keyword "
            "argument; got positional argument(s). Use "
            "@pytest.mark.allow_warnings(match=r'...') instead."
        )
    unknown_kwargs = sorted(set(marker.kwargs) - {"match"})
    if unknown_kwargs:
        raise TypeError(
            f"@pytest.mark.allow_warnings got unexpected keyword "
            f"argument(s): {unknown_kwargs}. The only supported keyword "
            "argument is 'match'. Use "
            "@pytest.mark.allow_warnings(match=r'...') instead."
        )
    match_arg = marker.kwargs.get("match")
    if match_arg is None:
        return None
    if not isinstance(match_arg, str):
        raise TypeError(
            f"@pytest.mark.allow_warnings `match` must be a str regex pattern or None; "
            f"got {type(match_arg).__name__}: {match_arg!r}"
        )
    try:
        return re.compile(match_arg)
    except re.error as exc:
        raise TypeError(
            f"@pytest.mark.allow_warnings was given an invalid regex for `match`: {match_arg!r} ({exc})"
        ) from exc


@pytest.fixture(autouse=True)
def fail_on_unexpected_loguru_warnings(
    request: pytest.FixtureRequest,
) -> Generator[None, None, None]:
    """Fail any test that emits a loguru WARNING-level (or higher) record.

    Opt-out mechanisms:
      * ``@pytest.mark.allow_warnings`` -- whole-test opt-out.
      * ``with allow_warnings(): ...`` -- fine-grained opt-out within a test.
      * Use of ``capture_loguru`` -- implicitly opts out for the duration of
        its context (since such tests are inspecting warnings on purpose).
      * Tests carrying any marker in ``_AUTO_ALLOW_WARNINGS_MARKERS`` are
        implicitly opted out. That set mixes external-resource markers (which
        produce a high noise of legitimate operational warnings) with
        higher-tier test markers (acceptance, release) that broadly tend to
        exercise external systems.
    """
    marker = request.node.get_closest_marker("allow_warnings")
    pushed_frame = False
    if marker is not None:
        pattern = _compile_allow_warnings_pattern(marker)
        WARNINGS_ALLOWED_STACK.append(pattern)
        pushed_frame = True
    elif any(request.node.get_closest_marker(m) is not None for m in _AUTO_ALLOW_WARNINGS_MARKERS):
        WARNINGS_ALLOWED_STACK.append(None)
        pushed_frame = True

    # Sink setup and the stack-frame pop must share a finally so that any
    # error during setup still pops the frame -- otherwise a setup failure
    # could leak state across tests within the same xdist worker process.
    sink_id: int | None = None
    try:
        _unexpected_warnings.clear()
        sink_id = logger.add(_unexpected_warning_sink, level="WARNING", format="{message}")
        yield
    finally:
        # Some tests call setup_logging() which invokes logger.remove() (no arg)
        # and removes all handlers including ours. In that case our sink is
        # already gone, so the resulting ValueError is expected; we still log
        # at trace level so it is observable when debugging.
        if sink_id is not None:
            try:
                logger.remove(sink_id)
            except ValueError as exc:
                logger.trace("Warning-detection sink {} was already removed: {}", sink_id, exc)
        if pushed_frame:
            WARNINGS_ALLOWED_STACK.pop()

    # Reached only when the test did not raise: a propagating exception from
    # ``yield`` would have run the finally above and then re-raised before
    # this point. Checking warnings here (rather than inside the finally)
    # avoids shadowing the test's own exception with our pytest.fail when a
    # test fails with both an exception and incidental unexpected warnings.
    if _unexpected_warnings:
        captured = list(_unexpected_warnings)
        _unexpected_warnings.clear()
        joined = "\n".join(f"  - {msg}" for msg in captured)
        pytest.fail(
            f"Test emitted {len(captured)} unexpected loguru WARNING-or-higher "
            f"record(s):\n{joined}\n"
            "Wrap the emitting code in `with allow_warnings():` (from "
            "imbue.mngr.utils.testing) or mark the test with "
            "@pytest.mark.allow_warnings if the warnings are expected.",
            pytrace=False,
        )
