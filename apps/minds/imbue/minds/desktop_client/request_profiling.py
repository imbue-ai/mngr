# One tracer span per Flask request, under the trace the page's ``traceparent`` header names.
#
# The span is named by the matched URL rule (``GET /api/v1/workspace-sharing/<workspace_id>``),
# so names are stable across requests, and it is the flow target of the page's request span:
# the header's parent span id is the per-hop ``bind_id`` the page set as its source. Routes
# that stream for as long as the session or the operation lasts, and the profiler's own
# ingestion endpoints, get no span: the one would dominate the timeline and the other would
# be noise about the tool. While profiling is off each hook costs one ``is_enabled`` check.

from contextlib import AbstractContextManager
from typing import Final

from flask import Flask
from flask import Response
from flask import g
from flask import request
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.profiling import SpanHandle
from imbue.imbue_common.profiling import get_profiler
from imbue.imbue_common.profiling import parse_traceparent
from imbue.imbue_common.pure import pure

TRACEPARENT_HEADER: Final[str] = "traceparent"
UNMATCHED_RULE: Final[str] = "<unmatched>"

STREAMING_RULES: Final[frozenset[str]] = frozenset(
    {
        "/ui/ws",
        "/api/v1/workspaces/operations/create/<operation_id>/logs",
        "/api/v1/workspaces/operations/destroy/<operation_id>/logs",
        "/api/v1/workspaces/operations/restart/<operation_id>/logs",
        "/api/v1/workspaces/operations/backup/<operation_id>/logs",
        "/api/v1/workspaces/operations/create-attempt-discard/<operation_id>/logs",
        "/api/v1/desktop/cloud-accounts/operations/<operation_id>/logs",
    }
)
_PROFILE_RULE_PREFIX: Final[str] = "/ui/api/profile/"


@pure
def is_rule_profiled(rule: str) -> bool:
    """Whether requests matching the rule get a span: not the long-lived streams, not the profiler's own endpoints."""
    return rule not in STREAMING_RULES and not rule.startswith(_PROFILE_RULE_PREFIX)


class _OpenRequestSpan(FrozenModel):
    """A request's span between before_request, which enters it, and teardown_request, which exits it."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    context_manager: AbstractContextManager[SpanHandle] = Field(description="The entered span, exited at teardown")
    handle: SpanHandle = Field(description="The span's handle, which the response status is added to")


def install_request_profiling(app: Flask) -> None:
    """Record one span per request on the process-wide profiler, named by the request's URL rule."""
    app.before_request(_open_request_span)
    app.after_request(_record_response_status)
    app.teardown_request(_close_request_span)


def _open_request_span() -> None:
    profiler = get_profiler()
    if not profiler.is_enabled:
        return
    rule = request.url_rule.rule if request.url_rule is not None else UNMATCHED_RULE
    if not is_rule_profiled(rule):
        return
    traceparent = request.headers.get(TRACEPARENT_HEADER)
    parent = parse_traceparent(traceparent)
    context_manager = profiler.span(
        f"{request.method} {rule}",
        traceparent=traceparent,
        flow_in=parent.span_id if parent is not None else None,
        path=request.path,
    )
    handle = context_manager.__enter__()
    handle.add_args(trace_id=handle.context.trace_id, span_id=handle.context.span_id)
    if parent is not None:
        handle.add_args(parent_span_id=parent.span_id)
    g.minds_profiling_request_span = _OpenRequestSpan(context_manager=context_manager, handle=handle)


def _record_response_status(response: Response) -> Response:
    if get_profiler().is_enabled:
        open_span: _OpenRequestSpan | None = g.get("minds_profiling_request_span")
        if open_span is not None:
            open_span.handle.add_args(status=response.status_code)
    return response


def _close_request_span(exc: BaseException | None) -> None:
    if not get_profiler().is_enabled:
        return
    open_span: _OpenRequestSpan | None = g.pop("minds_profiling_request_span", None)
    if open_span is None:
        return
    if exc is None:
        open_span.context_manager.__exit__(None, None, None)
    else:
        open_span.context_manager.__exit__(type(exc), exc, exc.__traceback__)
