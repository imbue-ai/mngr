# Turning what the page and the Electron shell post into events of the session's one trace file.
#
# The page's events arrive with ``performance.now()``-relative timestamps and are written under
# a synthetic process per page session (pids from 1,000,000 up, above any real one) on a single
# thread. Chromium's trace arrives in its own event format and is written as is, except that
# every URL among an event's arguments loses its query string and fragment, where a one-time
# login code or a share handoff token travels.

import re
import threading
from collections.abc import Mapping
from typing import Any
from typing import Final
from typing import assert_never

from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import TypeAdapter

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.profiling import ClockSync
from imbue.imbue_common.profiling import rebase_epoch_us
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.ui_models import UiProfileEvent
from imbue.minds.desktop_client.ui_models import UiProfileEventKind
from imbue.minds.desktop_client.ui_models import UiProfileEventsBatch

FIRST_PAGE_SESSION_PID: Final[int] = 1_000_000
PAGE_SESSION_TID: Final[int] = 1
PAGE_SESSION_THREAD_NAME: Final[str] = "main"

_URL_SCHEME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_URL_QUERY_AND_FRAGMENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"[?#].*$", re.DOTALL)


class PageSessionProcess(FrozenModel):
    """The synthetic process one page session's events are written under."""

    pid: int = Field(description="FIRST_PAGE_SESSION_PID plus the session's ordinal")
    ordinal: int = Field(description="The session's order of first appearance, from 1")
    is_first_batch: bool = Field(description="Whether this is the session's first batch, which names the process")


class PageSessionRegistry(MutableModel):
    """Assigns each page profiling session a synthetic pid above every real one, in order of first appearance."""

    _ordinal_by_session_id: dict[str, int] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def process_for(self, session_id: str) -> PageSessionProcess:
        with self._lock:
            known_ordinal = self._ordinal_by_session_id.get(session_id)
            ordinal = known_ordinal if known_ordinal is not None else len(self._ordinal_by_session_id) + 1
            self._ordinal_by_session_id[session_id] = ordinal
        return PageSessionProcess(
            pid=FIRST_PAGE_SESSION_PID + ordinal, ordinal=ordinal, is_first_batch=known_ordinal is None
        )


@pure
def page_process_name(ordinal: int, renderer_pid: int | None) -> str:
    """``Imbue Studio UI <n>``, naming the renderer's real pid when the Electron bridge reported one."""
    name = f"Imbue Studio UI {ordinal}"
    return f"{name} (renderer pid {renderer_pid})" if renderer_pid is not None else name


@pure
def _metadata_event(metadata_name: str, value: str, pid: int) -> dict[str, object]:
    return {"ph": "M", "name": metadata_name, "pid": pid, "tid": PAGE_SESSION_TID, "args": {"name": value}}


@pure
def _page_event_to_trace_event(
    event: UiProfileEvent, time_origin_ms: float, pid: int, clock_sync: ClockSync
) -> dict[str, object]:
    ts = rebase_epoch_us((time_origin_ms + event.start_ms) * 1000, clock_sync)
    match event.kind:
        case UiProfileEventKind.SPAN:
            trace_event: dict[str, object] = {
                "ph": "X",
                "name": event.name,
                "cat": event.cat,
                "ts": ts,
                "dur": round(event.duration_ms * 1000),
                "pid": pid,
                "tid": PAGE_SESSION_TID,
                "args": dict(event.args),
            }
        case UiProfileEventKind.INSTANT:
            trace_event = {
                "ph": "i",
                "s": "t",
                "name": event.name,
                "cat": event.cat,
                "ts": ts,
                "pid": pid,
                "tid": PAGE_SESSION_TID,
                "args": dict(event.args),
            }
        case _ as unreachable:
            assert_never(unreachable)
    if event.flow_out_id is not None:
        trace_event["bind_id"] = event.flow_out_id
        trace_event["flow_out"] = True
    if event.flow_in_id is not None:
        trace_event["bind_id"] = event.flow_in_id
        trace_event["flow_in"] = True
    return trace_event


@pure
def page_batch_to_trace_events(
    batch: UiProfileEventsBatch, process: PageSessionProcess, clock_sync: ClockSync
) -> list[dict[str, object]]:
    """Chrome trace events for one batch: the process and thread names on the session's first batch, then the events."""
    metadata_events = (
        [
            _metadata_event("process_name", page_process_name(process.ordinal, batch.renderer_pid), process.pid),
            _metadata_event("thread_name", PAGE_SESSION_THREAD_NAME, process.pid),
        ]
        if process.is_first_batch
        else []
    )
    return metadata_events + [
        _page_event_to_trace_event(event, batch.time_origin_ms, process.pid, clock_sync) for event in batch.events
    ]


@pure
def scrub_url_query_strings(value: object) -> object:
    """The value with the query string and fragment cut from every string in it that is a URL (a scheme, then ``://``)."""
    match value:
        case str() if _URL_SCHEME_PATTERN.match(value):
            return _URL_QUERY_AND_FRAGMENT_PATTERN.sub("", value)
        case dict():
            return {key: scrub_url_query_strings(item) for key, item in value.items()}
        case list():
            return [scrub_url_query_strings(item) for item in value]
        case _:
            return value


@pure
def scrub_chromium_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """The event verbatim, except that the URLs among its ``args`` lose their query strings and fragments."""
    scrubbed = dict(event)
    if "args" in scrubbed:
        scrubbed["args"] = scrub_url_query_strings(scrubbed["args"])
    return scrubbed


class ChromiumTraceDocument(FrozenModel):
    """Chromium's trace in its object form, as ``contentTracing`` writes it; only the events are kept."""

    model_config = ConfigDict(frozen=True, extra="ignore", arbitrary_types_allowed=False)

    trace_events: list[dict[str, Any]] = Field(alias="traceEvents", description="The events, verbatim")


CHROMIUM_TRACE_ADAPTER: TypeAdapter[ChromiumTraceDocument | list[dict[str, Any]]] = TypeAdapter(
    ChromiumTraceDocument | list[dict[str, Any]]
)


@pure
def chromium_trace_events(document: ChromiumTraceDocument | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The events of a Chromium trace posted in either form: the object with ``traceEvents``, or a bare array."""
    match document:
        case ChromiumTraceDocument():
            return document.trace_events
        case list():
            return document
        case _ as unreachable:
            assert_never(unreachable)
