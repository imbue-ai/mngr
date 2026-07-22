"""Flask server for foreman: agent list, transcript stream, and message send.

Single process, threaded werkzeug, ``use_reloader=False``, no auth (dev tool --
bind to a tailnet IP or firewall the port). Everything is per-connection state;
foreman assumes one user driving one box.
"""

from __future__ import annotations

import gzip
import json
import shlex
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any
from typing import Final

from flask import Flask
from flask import Response
from flask import jsonify
from flask import request
from flask.typing import ResponseReturnValue
from flask_sock import Sock
from loguru import logger

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.utils.thread_cleanup import cleanup_thread_local_resources
from imbue.mngr_foreman.agent_registry import AgentRegistry
from imbue.mngr_foreman.assets import ensure_assets
from imbue.mngr_foreman.assets import get_asset_dir
from imbue.mngr_foreman.connection_pool import ConnectionPool
from imbue.mngr_foreman.harness import TranscriptStrategy
from imbue.mngr_foreman.harness import transcript_strategy_for
from imbue.mngr_foreman.input_state import is_busy_state
from imbue.mngr_foreman.input_state import is_permissions_blocked
from imbue.mngr_foreman.input_state import probe_pane_state
from imbue.mngr_foreman.interrupt import InterruptError
from imbue.mngr_foreman.interrupt import send_interrupt_to_agent
from imbue.mngr_foreman.messaging import MessageSendError
from imbue.mngr_foreman.messaging import send_message_to_agent
from imbue.mngr_foreman.terminal import handle_host_shell_ws
from imbue.mngr_foreman.terminal import handle_orchestrator_ws
from imbue.mngr_foreman.terminal import handle_terminal_ws
from imbue.mngr_foreman.transcript_images import externalize_event_images
from imbue.mngr_foreman.transcript_images import get_cached_image
from imbue.mngr_foreman.transcript_tail import ReaderFn
from imbue.mngr_foreman.transcript_tail import SizeFn
from imbue.mngr_foreman.transcript_tail import TranscriptTailer
from imbue.mngr_foreman.uploads import MAX_UPLOAD_BYTES
from imbue.mngr_foreman.uploads import UploadError
from imbue.mngr_foreman.uploads import UploadNotFound
from imbue.mngr_foreman.uploads import content_type_for_name
from imbue.mngr_foreman.uploads import delete_upload
from imbue.mngr_foreman.uploads import read_upload
from imbue.mngr_foreman.uploads import write_upload

_STATIC_PACKAGE: Final[str] = "imbue.mngr_foreman.static"
# Characters allowed in a transcript-image id (uuid + tool_call_id + index).
_SAFE_IMAGE_ID_CHARS: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)
# Frequent heartbeats so the client's liveness watchdog notices a dead stream
# fast (a silently-dropped SSE otherwise looks "connected" but shows stale state).
# Sent as a real data event, not a ": comment" -- EventSource never dispatches
# comments to onmessage, so the client can't time them.
_HEARTBEAT_SECONDS: Final[float] = 5.0
# The transcript SSE loop polls at this fixed, consistent rate -- warm, never
# adaptive-idle. Cheap because a stat-before-read skips the read when the file
# hasn't grown (see TranscriptTailer), and the connection is always warm.
_TRANSCRIPT_POLL_SECONDS: Final[float] = 0.5
# Bound every foreground host command (stat/probe) so an unresponsive host can't
# wedge an SSE poll or request thread indefinitely.
_HOST_COMMAND_TIMEOUT_SECONDS: Final[float] = 10.0
# Tail-first backfill: parse only the last _BACKFILL_TAIL_LINES for the initial paint so
# a huge transcript loads its recent history in well under a second; the rest streams in
# afterward as "older" frames. _BACKFILL_PAINT_EVENTS is how many of the most recent
# events are painted before ``backfill_complete`` (the client fills the gap above from the
# "older" stream). Kept in lockstep with the client's TAIL_RENDER_COUNT.
_BACKFILL_TAIL_LINES: Final[int] = 400
_BACKFILL_PAINT_EVENTS: Final[int] = 40


def _read_static(rel_path: str, asset_dir: Path | None) -> tuple[bytes, str] | None:
    """Read a static asset by relative path, or None if missing/escaping.

    ``vendor/*`` third-party libs are served from the fetched asset cache
    (``asset_dir``, populated by ``ensure_assets``); ``vendor/atkinson.css`` and
    every non-vendor page/script ship in the package. A ``vendor/*`` file that is
    neither cached nor packaged returns None (404) -- the frontend degrades.
    """
    # Guard against path traversal: only allow simple forward-slashed names.
    parts = [p for p in rel_path.split("/") if p]
    if rel_path.startswith("/") or not parts or ".." in parts:
        return None
    if parts[0] == "vendor" and asset_dir is not None and len(parts) > 1:
        cached = asset_dir.joinpath(*parts[1:])
        try:
            if cached.is_file():
                return cached.read_bytes(), _content_type_for(parts[-1])
        except OSError:
            return None
    try:
        resource = resources.files(_STATIC_PACKAGE)
        for part in parts:
            resource = resource / part
        data = resource.read_bytes()
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None
    return data, _content_type_for(parts[-1])


def _content_type_for(filename: str) -> str:
    if filename.endswith(".html"):
        return "text/html; charset=utf-8"
    if filename.endswith(".css"):
        return "text/css; charset=utf-8"
    if filename.endswith(".js"):
        return "application/javascript; charset=utf-8"
    if filename.endswith(".json"):
        return "application/json; charset=utf-8"
    if filename.endswith(".woff2"):
        return "font/woff2"
    if filename.endswith(".woff"):
        return "font/woff"
    if filename.endswith(".svg"):
        return "image/svg+xml"
    return "application/octet-stream"


_COMPRESSIBLE_PREFIXES: Final[tuple[str, ...]] = ("text/", "application/javascript", "application/json", "image/svg")


def _is_compressible(content_type: str) -> bool:
    return content_type.startswith(_COMPRESSIBLE_PREFIXES)


def _sse(event_dict: dict) -> str:
    return f"data: {json.dumps(event_dict)}\n\n"


def _not_found() -> Response:
    return Response("Not found", status=404, mimetype="text/plain")


def _typed_response(data: bytes, content_type: str, headers: dict[str, str] | None = None) -> Response:
    """A Response with ``mimetype`` split from a ``type/subtype[; params]`` string."""
    return Response(data, mimetype=content_type.split(";")[0], content_type=content_type, headers=headers)


def _error(message: str, status: int) -> ResponseReturnValue:
    return jsonify({"ok": False, "error": message}), status


@contextmanager
def _thread_hub_cleanup() -> Iterator[None]:
    """Destroy this thread's gevent Hub when the wrapped block exits.

    SSE generators and WS handlers run for the life of the connection on their own
    thread and pop their request context early, so ``app.teardown_request`` (which
    handles this for ordinary requests) never fires for them.
    """
    try:
        yield
    finally:
        cleanup_thread_local_resources()


def create_app(
    mngr_ctx: MngrContext,
    registry: AgentRegistry,
    pool: ConnectionPool,
    max_tool_output_chars: int,
    asset_dir: Path | None = None,
) -> Flask:
    # static_folder=None: foreman serves its own bundled assets via importlib
    # resources (see _read_static), so Flask's default /static route would only
    # add a duplicate rule pointing at a non-existent folder.
    app = Flask(__name__, static_folder=None)
    # Reject oversize uploads at the framework edge (a little headroom over the
    # 25MB file cap for multipart overhead); write_upload re-checks the raw bytes.
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 1024 * 1024
    # Server-side WS ping/pong (flask_sock -> simple_websocket), off by default.
    # Without it, ws.receive() blocks on a plain socket.recv() with NO timeout: a
    # phone that drops off the network while backgrounded (radio killed, NAT/
    # carrier timeout -- no FIN or RST ever reaches us) leaves the terminal's
    # thread pair + forked tmux/ssh child + pty fds pinned forever (nothing times
    # them out; Linux's own idle-TCP keepalive is off by default too). Repeated
    # background/foreground cycles over a long session would accumulate one such
    # zombie per unclean drop. 25s ping (simple_websocket's own recommended
    # value) bounds a dead connection's lifetime to ~2 missed pongs (~50s worst
    # case) and needs no client change -- Ping/Pong are protocol-level control
    # frames the browser answers automatically, invisible to app.js.
    app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
    sock = Sock(app)

    # The dev server runs a fresh thread per request, and any request that touches
    # SSH via pyinfra spins up a thread-local gevent Hub whose wakeup pipe leaks
    # when that thread exits. Destroy it as each request finishes so those fds
    # can't pile up (which eventually pushes fd numbers past select()'s 1024 cap
    # and breaks terminals). No-op on threads that never touched gevent. Streaming
    # SSE generators and WS handlers pop their request context early, so they clean
    # up via _thread_hub_cleanup instead (see below); this covers the rest.
    @app.teardown_request
    def _cleanup_thread_hub(_exc: BaseException | None) -> None:
        cleanup_thread_local_resources()

    # ---- pages ------------------------------------------------------------

    @app.route("/")
    def index() -> Response:
        return _serve_static_or_404("index.html")

    @app.route("/a/<name>")
    def agent_page(name: str) -> Response:
        # The chat page is a static shell; it reads <name> from the URL itself.
        return _serve_static_or_404("agent.html")

    @app.route("/a/<name>/terminal")
    def terminal_page(name: str) -> Response:
        # The terminal page is a static shell; it reads <name> from the URL.
        return _serve_static_or_404("terminal.html")

    @app.route("/h/<host>/terminal")
    def host_terminal_page(host: str) -> Response:
        # Host shell: same static shell; JS reads the /h/<host>/ path and opens
        # the /ws/hosts/<host>/terminal websocket (a plain login shell on that box).
        return _serve_static_or_404("terminal.html")

    @app.route("/terminal")
    def orchestrator_terminal_page() -> Response:
        # Orchestrator shell: same static shell; JS detects the path and opens
        # the /ws/terminal websocket (a plain bash on the foreman host).
        return _serve_static_or_404("terminal.html")

    @app.route("/static/<path:rel_path>")
    def static_asset(rel_path: str) -> Response:
        return _serve_static_or_404(rel_path)

    def _serve_static_or_404(rel_path: str) -> Response:
        result = _read_static(rel_path, asset_dir)
        if result is None:
            return _not_found()
        data, content_type = result
        headers: dict[str, str] = {}
        # Vendored libs are pinned/versioned -> cache hard (the 3-4MB one-time cost
        # per device that was making cold loads slow). The app's own JS/CSS/HTML
        # change on deploy -> revalidate so updates propagate.
        if rel_path.startswith("vendor/"):
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            headers["Cache-Control"] = "no-cache"
        # Gzip compressible text assets on the fly when the client accepts it
        # (woff2/images are already compressed -> skip). Halves the first-load
        # bytes for the big JS libs before the immutable cache kicks in.
        if _is_compressible(content_type) and "gzip" in request.headers.get("Accept-Encoding", ""):
            data = gzip.compress(data, 6)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        return _typed_response(data, content_type, headers)

    # ---- agent list -------------------------------------------------------

    @app.route("/api/agents")
    def api_agents() -> Response:
        return jsonify({"agents": registry.snapshot()})

    @app.route("/api/agents/stream")
    def api_agents_stream() -> Response:
        def generate() -> Iterator[str]:
            with _thread_hub_cleanup():
                for message in registry.subscribe():
                    yield _sse(message)

        return Response(generate(), mimetype="text/event-stream", headers=_sse_headers())

    # ---- transcript -------------------------------------------------------

    @app.route("/api/agents/<name>/transcript")
    def api_transcript(name: str) -> Response:
        agent = registry.get_agent(name)
        if agent is None:
            return Response(
                _sse({"type": "error", "message": f"No agent named {name!r}"}),
                mimetype="text/event-stream",
                headers=_sse_headers(),
            )
        strategy = transcript_strategy_for(agent.type)
        if strategy is None:
            return Response(
                _sse({"type": "unsupported", "agent_type": agent.type}),
                mimetype="text/event-stream",
                headers=_sse_headers(),
            )

        return Response(
            _transcript_stream(agent, strategy, max_tool_output_chars, pool),
            mimetype="text/event-stream",
            headers=_sse_headers(),
        )

    @app.route("/api/agents/<name>/timage/<image_id>")
    def api_transcript_image(name: str, image_id: str) -> ResponseReturnValue:
        # Serve a large transcript image that was externalized out of its SSE
        # frame. Bytes come from the in-memory cache keyed by the parser's id.
        if not image_id or any(c not in _SAFE_IMAGE_ID_CHARS for c in image_id):
            return _not_found()
        cached = get_cached_image(image_id)
        if cached is None:
            return _not_found()
        media_type, raw = cached
        return _typed_response(raw, media_type, {"Cache-Control": "no-cache"})

    # ---- send message -----------------------------------------------------

    @app.route("/api/agents/<name>/message", methods=["POST"])
    def api_message(name: str) -> ResponseReturnValue:
        payload = request.get_json(silent=True) or {}
        message = payload.get("message", "")
        if not isinstance(message, str) or not message.strip():
            return _error("Message is empty", 400)
        try:
            send_message_to_agent(pool, name, message)
        except MessageSendError as e:
            # A blocking TUI dialog (permission / login) lands here -- surface it
            # so the UI can hint at the terminal page (phase 2).
            logger.info("Message to {} failed: {}", name, e)
            return _error(str(e), 502)
        return jsonify({"ok": True})

    @app.route("/api/agents/<name>/input-state")
    def api_input_state(name: str) -> ResponseReturnValue:
        # Cheap gate first: only a chat-supported agent has a meaningful blocking
        # state. The expensive tmux pane capture runs only past this gate, and only
        # for a harness that uses it.
        agent = registry.get_agent(name)
        strategy = transcript_strategy_for(agent.type) if agent is not None else None
        if agent is None or strategy is None:
            return jsonify(
                {"blocked": False, "reason": None, "running": False, "busy": False, "status": "READY", "state": None}
            )
        state = str(agent.state.value if hasattr(agent.state, "value") else agent.state).upper()
        if state not in ("RUNNING", "WAITING", "RUNNING_UNKNOWN_AGENT_TYPE"):
            return jsonify(
                {"blocked": False, "reason": None, "running": False, "busy": False, "status": "READY", "state": state}
            )
        # Three clean states, read from the LIVE pane for a pane-driven harness
        # (claude): NEEDS INPUT (a ❯ dialog on screen) beats WORKING (the title's
        # spinner glyph), else READY. Both come from ONE pane read, fresh every
        # poll -- so NEEDS INPUT clears the instant the dialog leaves the screen
        # (fixing the stuck-blocked bug from mngr's stale permissions marker), and
        # WORKING tracks the real generating state (not the interrupt-stale marker).
        if strategy.uses_pane_dialog_detection:
            working, reason = probe_pane_state(pool, name)
            if working is None and reason is None:
                # Pane unreadable THIS poll (slow/cold connection -- common for remote
                # docker agents). NEVER fall back to mngr's coarse state: it lies (reads
                # WAITING mid-turn), which is exactly the wrong dot we're replacing.
                # Return UNKNOWN and let the client keep the last-read dot until the next
                # poll reads the pane. The client polls ~1s, so the gap is tiny.
                return jsonify(
                    {"blocked": False, "reason": None, "running": True, "busy": False, "status": "UNKNOWN", "state": state}
                )
            busy = bool(working) and reason is None
        else:
            # codex/opencode/pi surface a permission block via mngr's field and
            # drive no run-time dialogs; use the marker signals for them.
            reason = "permission prompt" if is_permissions_blocked(agent) else None
            busy = is_busy_state(state)
        blocked = reason is not None
        status = "NEEDS_INPUT" if blocked else ("WORKING" if busy else "READY")
        return jsonify(
            {"blocked": blocked, "reason": reason, "running": True, "busy": busy, "status": status, "state": state}
        )

    @app.route("/api/agents/<name>/interrupt", methods=["POST"])
    def api_interrupt(name: str) -> ResponseReturnValue:
        # Send Escape to the agent's tmux pane (claude's "stop generating").
        try:
            send_interrupt_to_agent(mngr_ctx, name)
        except InterruptError as e:
            logger.info("Interrupt of {} failed: {}", name, e)
            return _error(str(e), 502)
        return jsonify({"ok": True})

    # ---- attachments ------------------------------------------------------

    @app.route("/api/agents/<name>/upload", methods=["POST"])
    def api_upload(name: str) -> ResponseReturnValue:
        # Multipart: the file plus the client-generated "<uuid>.<ext>" name. We
        # write it to <agent work_dir>/chat_uploads/ on the agent's host.
        if registry.get_agent(name) is None:
            return _error(f"No agent named {name!r}", 404)
        upload = request.files.get("file")
        stored_name = (request.form.get("filename") or "").strip()
        if upload is None or not stored_name:
            return _error("missing file or filename", 400)
        try:
            path = write_upload(pool, name, stored_name, upload.read())
        except UploadError as e:
            logger.info("Upload to {} failed: {}", name, e)
            return _error(str(e), 400)
        return jsonify({"ok": True, "path": path})

    @app.route("/api/agents/<name>/upload/<stored_name>", methods=["GET"])
    def api_get_upload(name: str, stored_name: str) -> ResponseReturnValue:
        # Serve an uploaded file's bytes back for inline rendering (image chips).
        if registry.get_agent(name) is None:
            return _not_found()
        try:
            data = read_upload(pool, name, stored_name)
        except UploadNotFound:
            return _not_found()
        except UploadError as e:
            return _error(str(e), 400)
        return _typed_response(data, content_type_for_name(stored_name), {"Cache-Control": "no-cache"})

    @app.route("/api/agents/<name>/upload/<stored_name>", methods=["DELETE"])
    def api_delete_upload(name: str, stored_name: str) -> ResponseReturnValue:
        try:
            delete_upload(pool, name, stored_name)
        except UploadError as e:
            logger.info("Upload delete for {} failed: {}", name, e)
            return _error(str(e), 400)
        return jsonify({"ok": True})

    # ---- terminal websocket ----------------------------------------------

    # Each WS handler touches SSH while building the terminal, on its own
    # per-connection thread -- see _thread_hub_cleanup.
    @sock.route("/ws/agents/<name>/terminal")
    def terminal_ws(ws: object, name: str) -> None:
        # Bridge the socket to the agent's tmux (direct ssh, mngr-connect fallback).
        with _thread_hub_cleanup():
            handle_terminal_ws(ws, name, pool)

    @sock.route("/ws/hosts/<host>/terminal")
    def host_shell_ws(ws: object, host: str) -> None:
        # A plain login shell on a known host (resolved via any agent on it).
        # handle_host_shell_ws closes the ws itself if the host can't be reached,
        # so a missing agent is passed through as an empty sentinel.
        with _thread_hub_cleanup():
            handle_host_shell_ws(ws, _first_agent_on_host(host) or "", host, pool)

    @sock.route("/ws/terminal")
    def orchestrator_ws(ws: object) -> None:
        # Bridge the socket to a plain `bash -l` on the foreman server machine.
        with _thread_hub_cleanup():
            handle_orchestrator_ws(ws)

    def _first_agent_on_host(host_name: str) -> str | None:
        for card in registry.snapshot():
            if card.get("host_name") == host_name:
                return str(card["name"])
        return None

    return app


def _sse_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }


def _build_transcript_reader(agent: AgentDetails, subpath: str, pool: ConnectionPool) -> tuple[ReaderFn, SizeFn]:
    """Build ``(reader, size_fn)`` over the agent's transcript, read through the warm pool.

    Both go through ``pool.run_on_host`` so they reuse the always-warm, already-resolved
    host connection the keepalive maintains. Resolving a fresh readable host per open
    (``provider.get_host`` runs a live online probe) cost ~5s of cold latency on *every*
    transcript load; the pool already holds a hot handle for every live agent, so we drop
    straight onto it (the same path the input-state probe takes in ~20ms). The transcript
    lives at ``<host_dir>/agents/<id>/<subpath>`` -- the exact location the events target
    resolves to. A dropped connection surfaces as an empty read here and is re-resolved by
    the pool's keepalive, so the next poll recovers with no per-reader refresh bookkeeping.
    """
    agent_name = str(agent.name)
    rel_path = Path("agents") / str(agent.id) / subpath

    def _abs_path(host: OnlineHostInterface) -> Path:
        return host.host_dir / rel_path

    def reader() -> bytes:
        def _read(_agent: AgentInterface, host: OnlineHostInterface) -> bytes:
            return host.read_file(_abs_path(host))

        try:
            return pool.run_on_host(agent_name, _read)
        except Exception as e:  # noqa: BLE001 - a failed read just yields no new lines this poll
            logger.trace("transcript read for {} failed (retrying next poll): {}", agent_name, e)
            return b""

    def size_fn() -> int | None:
        def _stat(_agent: AgentInterface, host: OnlineHostInterface) -> int | None:
            result = host.execute_stateful_command(
                f"stat -c %s {shlex.quote(str(_abs_path(host)))} 2>/dev/null",
                timeout_seconds=_HOST_COMMAND_TIMEOUT_SECONDS,
            )
            out = (result.stdout or "").strip()
            return int(out) if result.success and out.isdigit() else None

        try:
            return pool.run_on_host(agent_name, _stat)
        except Exception:  # noqa: BLE001 - a failed stat just means "read anyway"
            return None

    return reader, size_fn


def _transcript_stream(
    agent: AgentDetails,
    strategy: TranscriptStrategy,
    max_tool_output_chars: int,
    pool: ConnectionPool,
) -> Iterator[str]:
    """SSE generator: full backfill, then live events, with periodic heartbeats."""
    with _thread_hub_cleanup():
        yield from _transcript_stream_inner(agent, strategy, max_tool_output_chars, pool)


def _transcript_stream_inner(
    agent: AgentDetails,
    strategy: TranscriptStrategy,
    max_tool_output_chars: int,
    pool: ConnectionPool,
) -> Iterator[str]:
    reader, size_fn = _build_transcript_reader(agent, strategy.subpath, pool)

    tailer = TranscriptTailer(reader, size_fn=size_fn)
    existing_event_ids: set[str] = set()
    tool_name_by_call_id: dict[str, str] = {}
    # Live message-queue FIFO, replayed from the transcript's queue-operation lines
    # so queued messages appear the instant they're enqueued (see the parser).
    queue_state: list[dict[str, Any]] = []

    def _parse(lines: list[str], queue: list[dict[str, Any]]) -> list[dict]:
        if not lines:
            return []
        return list(
            strategy.parse(
                lines,
                existing_event_ids=existing_event_ids,
                tool_name_by_call_id=tool_name_by_call_id,
                max_tool_output_chars=max_tool_output_chars,
                queue_state=queue,
            )
        )

    def _frames(events: list[dict], kind: str) -> Iterator[str]:
        for event in events:
            # Move large base64 images out-of-band so SSE frames stay small; the
            # client fetches them by id from the /timage endpoint.
            externalize_event_images(event)
            yield _sse({"type": kind, "event": event})

    # Read the whole file once (cheap I/O over the warm host); the tailer offset is now
    # at EOF so live-follow below only sees genuinely new lines.
    try:
        backfill_lines = tailer.poll()
    except Exception as e:  # noqa: BLE001
        yield _sse({"type": "error", "message": f"Failed to read transcript: {e}"})
        return

    # Tail-first: parse only the recent tail and paint the last few messages immediately,
    # then stream the rest of history above them (newest-first). A 9 MB / 2000-event
    # transcript otherwise ships+parses+paints everything before the first byte renders
    # (~10s in the browser); this makes the current conversation appear in well under 1s.
    tail_lines = backfill_lines[-_BACKFILL_TAIL_LINES:]
    head_lines = backfill_lines[: len(backfill_lines) - len(tail_lines)]
    tail_events = _parse(tail_lines, queue_state)  # the live queue derives from recent ops
    paint = tail_events[-_BACKFILL_PAINT_EVENTS:]
    region_older = tail_events[: len(tail_events) - len(paint)]

    yield from _frames(paint, "event")
    yield _sse({"type": "backfill_complete"})

    # Older history, newest-first so the client prepends each event straight onto the top.
    # The head is parsed with a throwaway queue so its long-resolved queue-ops can't
    # perturb the live queue built from the tail; ids/tool-names stay shared for dedup.
    yield from _frames(list(reversed(region_older)), "older")
    yield from _frames(list(reversed(_parse(head_lines, []))), "older")
    yield _sse({"type": "older_complete"})

    # Live follow at a fixed, consistent fast rate -- the connection is always warm
    # and a stat-before-read keeps a poll cheap when the file hasn't grown.
    last_heartbeat = time.monotonic()
    while True:
        time.sleep(_TRANSCRIPT_POLL_SECONDS)
        try:
            new_lines = tailer.poll()
        except Exception as e:  # noqa: BLE001
            logger.trace("Transcript poll error (continuing): {}", e)
            new_lines = []
        yield from _frames(_parse(new_lines, queue_state), "event")
        now = time.monotonic()
        if now - last_heartbeat >= _HEARTBEAT_SECONDS:
            yield _sse({"type": "heartbeat"})
            last_heartbeat = now


def run_server(
    mngr_ctx: MngrContext,
    host: str,
    port: int,
    max_tool_output_chars: int,
) -> None:
    """Start the registry + warm connection pool and serve forever (blocking)."""
    # Fetch pinned frontend libs into the local cache before serving (a no-op
    # once cached). Never fatal: missing assets degrade the UI, they don't stop
    # the server -- see ensure_assets.
    asset_dir = get_asset_dir(mngr_ctx)
    ensure_assets(asset_dir)
    registry = AgentRegistry(mngr_ctx)
    pool = ConnectionPool(mngr_ctx)
    # Start the warm pool first so its change-callback is registered before the
    # registry's first poll; then start the registry (a background discovery poll
    # loop -- does not block the port bind below). The first poll fills the list a
    # few seconds later and wakes the pool to warm the live agents.
    pool.start_maintainer(registry)
    registry.start()
    app = create_app(mngr_ctx, registry, pool, max_tool_output_chars, asset_dir=asset_dir)
    # threaded=True so SSE connections (one long-lived generator each) don't
    # block the list/message endpoints; use_reloader=False (single process).
    app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
