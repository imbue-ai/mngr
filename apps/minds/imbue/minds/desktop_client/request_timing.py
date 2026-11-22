# One debug line per API request, saying how long the backend took to answer it.
#
# A slow page is usually one slow route, and the log is what a developer has
# after the fact: the profiler (``request_profiling.py``) records the same
# requests in far more detail, but only while a profile is being taken. The
# line carries the path and never the query string, which can hold tokens.

import time
from typing import Final

from flask import Flask
from flask import Response
from flask import g
from flask import request
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.request_profiling import UNMATCHED_RULE
from imbue.minds.desktop_client.request_profiling import is_rule_profiled

_TIMED_RULE_PREFIXES: Final[tuple[str, ...]] = ("/api/", "/ui/api/")


@pure
def is_rule_timed(rule: str) -> bool:
    """Whether requests matching the rule get a line: the API routes, less the long-lived streams and the profiler's own endpoints."""
    return rule.startswith(_TIMED_RULE_PREFIXES) and is_rule_profiled(rule)


def install_request_timing(app: Flask) -> None:
    """Log each API request's method, path, status, and duration at debug level."""
    app.before_request(_note_request_start)
    app.after_request(_note_response_status)
    app.teardown_request(_log_request_duration)


def _note_request_start() -> None:
    rule = request.url_rule.rule if request.url_rule is not None else UNMATCHED_RULE
    if is_rule_timed(rule):
        g.minds_request_started_at = time.monotonic()


def _note_response_status(response: Response) -> Response:
    g.minds_request_status = response.status_code
    return response


def _log_request_duration(exc: BaseException | None) -> None:
    started_at: float | None = g.pop("minds_request_started_at", None)
    if started_at is None:
        return
    status: int | None = g.pop("minds_request_status", None)
    outcome = str(status) if status is not None else f"raised {type(exc).__name__}"
    logger.debug(
        "{} {} -> {} in {:.0f} ms", request.method, request.path, outcome, (time.monotonic() - started_at) * 1000
    )
