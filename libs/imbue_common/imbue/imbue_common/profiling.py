import atexit
import functools
import json
import os
import random
import re
import sys
import threading
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import AbstractContextManager
from contextlib import contextmanager
from contextlib import nullcontext
from pathlib import Path
from typing import Final
from typing import ParamSpec
from typing import TextIO
from typing import TypeVar

from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.errors import SwitchError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

_TRACE_ID_HEX_LENGTH: Final[int] = 32
_SPAN_ID_HEX_LENGTH: Final[int] = 16
_ZERO_TRACE_ID: Final[str] = "0" * _TRACE_ID_HEX_LENGTH
_ZERO_SPAN_ID: Final[str] = "0" * _SPAN_ID_HEX_LENGTH
_TRACEPARENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")

_DEFAULT_CATEGORY: Final[str] = "app"
_PROFILER_CATEGORY: Final[str] = "profiling"
_OVERFLOW_EVENT_NAME: Final[str] = "overflow"
_WRITER_MAX_EVENT_COUNT: Final[int] = 1_000_000
_COLLECTOR_MAX_EVENT_COUNT: Final[int] = 50_000
_FLUSH_EVENT_COUNT: Final[int] = 512
_FLUSH_AGE_US: Final[int] = 1_000_000


class ProfilingError(Exception):
    """Base class for errors raised by the profiling module."""


class ProfilingModeError(ProfilingError):
    """Raised when an operation needs a profiling mode other than the one in effect."""


class ProfilingDisabledError(ProfilingError):
    """Raised when a value that exists only while profiling is enabled is read from the no-op profiler."""


class ProfilingFlowError(ProfilingError, ValueError):
    """Raised when a span is asked to join two different flows, which one event cannot express."""


class ProfilingTimestampError(ProfilingError, ValueError):
    """Raised when an already-finished span is recorded with an end before its start."""


def now_us() -> int:
    """Read the trace clock: the system monotonic clock in integer microseconds."""
    return time.perf_counter_ns() // 1000


def mint_trace_id() -> str:
    candidate = f"{random.getrandbits(_TRACE_ID_HEX_LENGTH * 4):0{_TRACE_ID_HEX_LENGTH}x}"
    return candidate if candidate != _ZERO_TRACE_ID else mint_trace_id()


def mint_span_id() -> str:
    candidate = f"{random.getrandbits(_SPAN_ID_HEX_LENGTH * 4):0{_SPAN_ID_HEX_LENGTH}x}"
    return candidate if candidate != _ZERO_SPAN_ID else mint_span_id()


class TraceContext(FrozenModel):
    """The trace a span belongs to and the span itself, as W3C Trace Context ids."""

    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$", description="Id shared by every span of one trace")
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$", description="Id of this span within the trace")


@pure
def parse_traceparent(value: str | None) -> TraceContext | None:
    """Parse a W3C traceparent header, treating anything malformed, unknown, or all-zero as absent."""
    if value is None:
        return None
    match = _TRACEPARENT_PATTERN.match(value)
    if match is None:
        return None
    trace_id, span_id = match.group(1), match.group(2)
    if trace_id == _ZERO_TRACE_ID or span_id == _ZERO_SPAN_ID:
        return None
    return TraceContext(trace_id=trace_id, span_id=span_id)


@pure
def render_traceparent(context: TraceContext) -> str:
    return f"00-{context.trace_id}-{context.span_id}-01"


class ClockSync(FrozenModel):
    """One simultaneous reading of the wall clock and the trace clock, for rebasing foreign timestamps."""

    epoch_ns: int = Field(description="time.time_ns() at the sync point")
    perf_counter_ns: int = Field(description="time.perf_counter_ns() at the same sync point")


def capture_clock_sync() -> ClockSync:
    return ClockSync(epoch_ns=time.time_ns(), perf_counter_ns=time.perf_counter_ns())


@pure
def rebase_epoch_us(epoch_us: float, sync: ClockSync) -> int:
    """Move a wall-clock microsecond timestamp onto the trace clock through a sync point."""
    return int(round(sync.perf_counter_ns // 1000 + (epoch_us - sync.epoch_ns / 1000)))


# Event construction


@pure
def _build_span_event(
    name: str,
    cat: str,
    ts: int,
    dur: int,
    pid: int,
    tid: int,
    args: Mapping[str, object],
    flow_in: str | None,
    flow_out: str | None,
) -> dict[str, object]:
    """Build a complete ("X") event, with the v2 flow link fields when the span joins a flow."""
    event: dict[str, object] = {
        "ph": "X",
        "name": name,
        "cat": cat,
        "ts": ts,
        "dur": dur,
        "pid": pid,
        "tid": tid,
        "args": dict(args),
    }
    _ensure_flows_agree(name, flow_in, flow_out)
    bind_id = flow_in if flow_in is not None else flow_out
    if bind_id is not None:
        event["bind_id"] = bind_id
    if flow_in is not None:
        event["flow_in"] = True
    if flow_out is not None:
        event["flow_out"] = True
    return event


@pure
def _ensure_flows_agree(name: str, flow_in: str | None, flow_out: str | None) -> None:
    """One event carries one bind_id, so a span that is both a target and a source must name one flow."""
    if flow_in is not None and flow_out is not None and flow_in != flow_out:
        raise ProfilingFlowError(
            f"Span {name!r} cannot be the target of flow {flow_in!r} and the source of {flow_out!r}"
        )


@pure
def _build_instant_event(
    name: str, cat: str, ts: int, pid: int, tid: int, args: Mapping[str, object]
) -> dict[str, object]:
    return {"ph": "i", "s": "t", "name": name, "cat": cat, "ts": ts, "pid": pid, "tid": tid, "args": dict(args)}


@pure
def _build_metadata_event(metadata_name: str, value: str, pid: int, tid: int) -> dict[str, object]:
    return {"ph": "M", "name": metadata_name, "pid": pid, "tid": tid, "args": {"name": value}}


@pure
def _default_process_name(argv: Sequence[str]) -> str:
    """Name a process after its executable's base name and its first argument, the way the spec lists processes."""
    executable_name = Path(argv[0]).name if argv else ""
    return f"{executable_name} {argv[1]}" if len(argv) > 1 else executable_name


@pure
def _child_context(parent: TraceContext | None) -> TraceContext:
    """Open a span under its parent's trace, or under a freshly minted trace when there is no parent."""
    trace_id = parent.trace_id if parent is not None else mint_trace_id()
    return TraceContext(trace_id=trace_id, span_id=mint_span_id())


# Span handles


class SpanHandle(MutableModel, ABC):
    """What an open span yields: its trace context, and a way to add arguments before the span is recorded."""

    @property
    @abstractmethod
    def context(self) -> TraceContext:
        """The trace id and span id of the open span."""

    @abstractmethod
    def add_args(self, **args: object) -> None:
        """Merge arguments into the event recorded when the span exits."""

    @abstractmethod
    def set_flow_out(self, bind_id: str) -> None:
        """Make the span the source of the given flow; allowed any time before it exits."""

    @abstractmethod
    def set_flow_in(self, bind_id: str) -> None:
        """Make the span the target of the given flow; allowed any time before it exits."""


class _RecordingSpanHandle(SpanHandle):
    """The handle of a span that will be recorded on exit."""

    name: str = Field(frozen=True, description="The span's name, for the error naming a torn flow")
    span_context: TraceContext = Field(frozen=True, description="The open span's trace and span ids")
    recorded_args: dict[str, object] = Field(default_factory=dict, description="Arguments recorded with the span")
    flow_in: str | None = Field(default=None, description="The flow this span is the target of, if any")
    flow_out: str | None = Field(default=None, description="The flow this span is the source of, if any")

    @property
    def context(self) -> TraceContext:
        return self.span_context

    def add_args(self, **args: object) -> None:
        self.recorded_args.update(args)

    def set_flow_out(self, bind_id: str) -> None:
        _ensure_flows_agree(self.name, self.flow_in, bind_id)
        self.flow_out = bind_id

    def set_flow_in(self, bind_id: str) -> None:
        _ensure_flows_agree(self.name, bind_id, self.flow_out)
        self.flow_in = bind_id


class _DisabledSpanHandle(SpanHandle):
    """The handle every span yields while profiling is off; it has no context to give."""

    @property
    def context(self) -> TraceContext:
        raise ProfilingDisabledError(
            "A span has no trace context while profiling is disabled; check get_profiler().is_enabled before reading it"
        )

    def add_args(self, **args: object) -> None:
        pass

    def set_flow_out(self, bind_id: str) -> None:
        pass

    def set_flow_in(self, bind_id: str) -> None:
        pass


# Profilers


class ProfilerInterface(MutableModel, ABC):
    """Records spans and instants for one process as Chrome trace events, keeping a per-thread trace context."""

    @property
    @abstractmethod
    def is_enabled(self) -> bool:
        """Whether a mode is enabled, so that span names worth formatting only when recorded can be skipped."""

    @property
    @abstractmethod
    def clock_sync(self) -> ClockSync:
        """The wall-clock and trace-clock pair captured when the mode was enabled."""

    @abstractmethod
    def span(
        self,
        name: str,
        cat: str = _DEFAULT_CATEGORY,
        flow_in: str | None = None,
        flow_out: str | None = None,
        traceparent: str | None = None,
        **args: object,
    ) -> AbstractContextManager[SpanHandle]:
        """Open a span that is recorded as a complete event when the context manager exits."""

    @abstractmethod
    def instant(self, name: str, cat: str = _DEFAULT_CATEGORY, **args: object) -> None:
        """Record a thread-scoped instant event at the current time."""

    @abstractmethod
    def complete(self, name: str, start_us: int, end_us: int, cat: str = _DEFAULT_CATEGORY, **args: object) -> None:
        """Record a span that already finished, with explicit trace-clock timestamps, on the current thread."""

    @abstractmethod
    def current_traceparent(self) -> str | None:
        """Render the innermost open span on this thread as a traceparent header, or None when there is none."""

    @abstractmethod
    def set_process_name(self, name: str) -> None:
        """Record the process_name metadata event; the last one recorded wins in the viewer."""

    @abstractmethod
    def write_foreign_events(self, events: Sequence[Mapping[str, object]]) -> None:
        """Append events another process recorded, verbatim and uncounted; only the writer accepts them."""

    @abstractmethod
    def flush(self) -> None:
        """Write everything buffered so far to the trace file, when there is one."""


class _ThreadTraceStack(threading.local):
    """The calling thread's open spans, innermost last; a new thread starts with none open."""

    contexts: tuple[TraceContext, ...] = ()


class _RecordingProfiler(ProfilerInterface, ABC):
    """The recording shared by both modes: event building, the per-thread context, thread names, and the cap."""

    clock_sync_at_enable: ClockSync = Field(frozen=True, description="The clock pair captured when enabled")
    max_event_count: int = Field(frozen=True, description="Own events recorded before the rest are dropped")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _pid: int = PrivateAttr(default_factory=os.getpid)
    _thread_stack: _ThreadTraceStack = PrivateAttr(default_factory=_ThreadTraceStack)
    _named_thread_ids: set[int] = PrivateAttr(default_factory=set)
    _recorded_event_count: int = PrivateAttr(default=0)
    _is_overflowed: bool = PrivateAttr(default=False)

    @property
    def is_enabled(self) -> bool:
        return True

    @property
    def clock_sync(self) -> ClockSync:
        return self.clock_sync_at_enable

    @contextmanager
    def span(
        self,
        name: str,
        cat: str = _DEFAULT_CATEGORY,
        flow_in: str | None = None,
        flow_out: str | None = None,
        traceparent: str | None = None,
        **args: object,
    ) -> Generator[SpanHandle, None, None]:
        supplied_parent = parse_traceparent(traceparent)
        enclosing_contexts = self._thread_stack.contexts
        parent = supplied_parent if supplied_parent is not None else _top_context(enclosing_contexts)
        handle = _RecordingSpanHandle(
            name=name, span_context=_child_context(parent), recorded_args=args, flow_in=flow_in, flow_out=flow_out
        )
        self._thread_stack.contexts = enclosing_contexts + (handle.span_context,)
        start_us = now_us()
        is_failed = True
        try:
            yield handle
            is_failed = False
        finally:
            duration_us = now_us() - start_us
            self._thread_stack.contexts = enclosing_contexts
            if is_failed:
                handle.recorded_args["is_failed"] = True
            tid = threading.get_native_id()
            self._record_own_event(
                _build_span_event(
                    name,
                    cat,
                    start_us,
                    duration_us,
                    self._pid,
                    tid,
                    handle.recorded_args,
                    handle.flow_in,
                    handle.flow_out,
                ),
                tid,
            )

    def instant(self, name: str, cat: str = _DEFAULT_CATEGORY, **args: object) -> None:
        tid = threading.get_native_id()
        self._record_own_event(_build_instant_event(name, cat, now_us(), self._pid, tid, args), tid)

    def complete(self, name: str, start_us: int, end_us: int, cat: str = _DEFAULT_CATEGORY, **args: object) -> None:
        if end_us < start_us:
            raise ProfilingTimestampError(f"Span {name!r} ends at {end_us} us, before it starts at {start_us} us")
        tid = threading.get_native_id()
        self._record_own_event(
            _build_span_event(name, cat, start_us, end_us - start_us, self._pid, tid, args, None, None), tid
        )

    def current_traceparent(self) -> str | None:
        top = _top_context(self._thread_stack.contexts)
        return render_traceparent(top) if top is not None else None

    def set_process_name(self, name: str) -> None:
        event = _build_metadata_event("process_name", name, self._pid, threading.get_native_id())
        with self._lock:
            self._store_metadata_event_locked(event)

    def _record_own_event(self, event: dict[str, object], tid: int) -> None:
        """Store one of this process's events, naming its thread first and honoring the cap exactly once."""
        with self._lock:
            if tid not in self._named_thread_ids:
                self._named_thread_ids.add(tid)
                thread_name = threading.current_thread().name
                self._store_metadata_event_locked(_build_metadata_event("thread_name", thread_name, self._pid, tid))
            if self._recorded_event_count >= self.max_event_count:
                if not self._is_overflowed:
                    self._is_overflowed = True
                    overflow_args = {"max_event_count": self.max_event_count}
                    self._store_event_locked(
                        _build_instant_event(
                            _OVERFLOW_EVENT_NAME, _PROFILER_CATEGORY, now_us(), self._pid, tid, overflow_args
                        )
                    )
                return
            self._recorded_event_count += 1
            self._store_event_locked(event)

    @abstractmethod
    def _store_event_locked(self, event: dict[str, object]) -> None:
        """Keep a span, instant, or overflow event; called with the lock held."""

    @abstractmethod
    def _store_metadata_event_locked(self, event: dict[str, object]) -> None:
        """Keep a process_name or thread_name event; called with the lock held."""


@pure
def _top_context(contexts: tuple[TraceContext, ...]) -> TraceContext | None:
    return contexts[-1] if contexts else None


class _CollectorProfiler(_RecordingProfiler):
    """Keeps events in memory for another process to write, handing metadata over first."""

    _metadata_events: list[dict[str, object]] = PrivateAttr(default_factory=list)
    _events: list[dict[str, object]] = PrivateAttr(default_factory=list)

    def write_foreign_events(self, events: Sequence[Mapping[str, object]]) -> None:
        raise ProfilingModeError("Only the writer accepts foreign events; this process is collecting")

    def flush(self) -> None:
        pass

    def drain(self) -> list[dict[str, object]]:
        with self._lock:
            drained = self._metadata_events + self._events
            self._metadata_events.clear()
            self._events.clear()
        return drained

    def _store_event_locked(self, event: dict[str, object]) -> None:
        self._events.append(event)

    def _store_metadata_event_locked(self, event: dict[str, object]) -> None:
        self._metadata_events.append(event)


class _WriterProfiler(_RecordingProfiler):
    """Appends events to the trace file as a JSON array, flushing by count, by age, and at close."""

    path: Path = Field(frozen=True, description="The trace file, overwritten when opened")
    clock_us: Callable[[], int] = Field(
        default=now_us, frozen=True, description="Clock the flush age is measured on, replaced in tests"
    )

    _file: TextIO | None = PrivateAttr(default=None)
    _buffer: list[dict[str, object]] = PrivateAttr(default_factory=list)
    _written_event_count: int = PrivateAttr(default=0)
    _last_flush_us: int = PrivateAttr(default=0)
    _is_closed: bool = PrivateAttr(default=False)

    def open(self) -> None:
        """Create the file and start the array; events are only kept once this has run."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._file = self.path.open("w", encoding="utf-8")
            self._file.write("[")
            self._file.flush()
            self._last_flush_us = self.clock_us()

    def close(self) -> None:
        """Flush, terminate the array, and close the file; later calls and later events do nothing."""
        with self._lock:
            if self._file is None or self._is_closed:
                return
            self._flush_locked()
            self._file.write("\n]\n")
            self._file.close()
            self._is_closed = True

    def write_foreign_events(self, events: Sequence[Mapping[str, object]]) -> None:
        with self._lock:
            if not self._is_accepting_locked():
                return
            self._buffer.extend(dict(event) for event in events)
            self._flush_if_due_locked()

    def flush(self) -> None:
        with self._lock:
            if self._is_accepting_locked():
                self._flush_locked()

    def _store_event_locked(self, event: dict[str, object]) -> None:
        if not self._is_accepting_locked():
            return
        self._buffer.append(event)
        self._flush_if_due_locked()

    def _store_metadata_event_locked(self, event: dict[str, object]) -> None:
        self._store_event_locked(event)

    def _is_accepting_locked(self) -> bool:
        return self._file is not None and not self._is_closed

    def _flush_if_due_locked(self) -> None:
        if len(self._buffer) >= _FLUSH_EVENT_COUNT or self.clock_us() - self._last_flush_us >= _FLUSH_AGE_US:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._file is None:
            raise ProfilingModeError("The writer's file is not open")
        for event in self._buffer:
            if self._written_event_count > 0:
                self._file.write(",\n")
            self._file.write(json.dumps(event, separators=(",", ":"), default=str))
            self._written_event_count += 1
        self._buffer.clear()
        self._file.flush()
        self._last_flush_us = self.clock_us()


class _DisabledProfiler(ProfilerInterface):
    """The profiler in effect until a mode is enabled; every call is a no-op that allocates nothing."""

    @property
    def is_enabled(self) -> bool:
        return False

    @property
    def clock_sync(self) -> ClockSync:
        raise ProfilingDisabledError("There is no clock sync while profiling is disabled")

    def span(
        self,
        name: str,
        cat: str = _DEFAULT_CATEGORY,
        flow_in: str | None = None,
        flow_out: str | None = None,
        traceparent: str | None = None,
        **args: object,
    ) -> AbstractContextManager[SpanHandle]:
        return _DISABLED_SPAN

    def instant(self, name: str, cat: str = _DEFAULT_CATEGORY, **args: object) -> None:
        pass

    def complete(self, name: str, start_us: int, end_us: int, cat: str = _DEFAULT_CATEGORY, **args: object) -> None:
        pass

    def current_traceparent(self) -> str | None:
        return None

    def set_process_name(self, name: str) -> None:
        pass

    def write_foreign_events(self, events: Sequence[Mapping[str, object]]) -> None:
        raise ProfilingModeError("Only the writer accepts foreign events; profiling is disabled")

    def flush(self) -> None:
        pass


_DISABLED_SPAN: Final[AbstractContextManager[SpanHandle]] = nullcontext(_DisabledSpanHandle())
_DISABLED_PROFILER: Final[ProfilerInterface] = _DisabledProfiler()


class _ActiveProfilerSlot(MutableModel):
    """The one process-wide profiler, swapped by enable_writer, enable_collector, and disable."""

    profiler: ProfilerInterface = Field(description="What get_profiler() returns")


_ACTIVE_PROFILER: Final[_ActiveProfilerSlot] = _ActiveProfilerSlot(profiler=_DISABLED_PROFILER)


def get_profiler() -> ProfilerInterface:
    return _ACTIVE_PROFILER.profiler


def enable_writer(path: Path, process_name: str | None = None) -> None:
    """Put this process's profiler in writer mode, owning the trace file at the given path."""
    _ensure_no_mode_is_enabled()
    writer = _WriterProfiler(
        path=path, clock_sync_at_enable=capture_clock_sync(), max_event_count=_WRITER_MAX_EVENT_COUNT
    )
    writer.open()
    atexit.register(writer.close)
    writer.set_process_name(process_name if process_name is not None else _default_process_name(sys.argv))
    _ACTIVE_PROFILER.profiler = writer


def enable_collector(process_name: str | None = None) -> None:
    """Put this process's profiler in collector mode, keeping events for drain_events()."""
    _ensure_no_mode_is_enabled()
    collector = _CollectorProfiler(
        clock_sync_at_enable=capture_clock_sync(), max_event_count=_COLLECTOR_MAX_EVENT_COUNT
    )
    collector.set_process_name(process_name if process_name is not None else _default_process_name(sys.argv))
    _ACTIVE_PROFILER.profiler = collector


def drain_events() -> list[dict[str, object]]:
    """Return and clear the collector's events, metadata first; raises ProfilingModeError in any other mode."""
    match _ACTIVE_PROFILER.profiler:
        case _CollectorProfiler() as collector:
            return collector.drain()
        case _:
            raise ProfilingModeError("Events can only be drained in collector mode")


def disable() -> None:
    """Close the writer or drop the collector, then return to the no-op profiler; harmless when nothing is enabled."""
    profiler = _ACTIVE_PROFILER.profiler
    _ACTIVE_PROFILER.profiler = _DISABLED_PROFILER
    match profiler:
        case _WriterProfiler() as writer:
            atexit.unregister(writer.close)
            writer.close()
        case _CollectorProfiler() | _DisabledProfiler():
            pass
        case _:
            raise SwitchError(f"Unexpected profiler type: {type(profiler).__name__}")


def _ensure_no_mode_is_enabled() -> None:
    if _ACTIVE_PROFILER.profiler.is_enabled:
        raise ProfilingModeError("A profiling mode is already enabled; call disable() first")


# The profiled decorator


P = ParamSpec("P")
R = TypeVar("R")


class _ProfiledDecorator(FrozenModel):
    """The decorator profiled() returns; it consults the live profiler on every call, not at decoration time."""

    name: str = Field(description="Name of the span opened around each call")
    cat: str = Field(description="Category of that span")

    def __call__(self, func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with get_profiler().span(self.name, self.cat):
                return func(*args, **kwargs)

        return wrapper


def profiled(name: str, cat: str = _DEFAULT_CATEGORY) -> _ProfiledDecorator:
    """Decorate a function so each call runs inside a span of the given name on whichever profiler is live."""
    return _ProfiledDecorator(name=name, cat=cat)
