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
from importlib import resources
from pathlib import Path
from typing import Final

from flask import Flask
from flask import Response
from flask import jsonify
from flask import request
from flask.typing import ResponseReturnValue
from flask_sock import Sock
from loguru import logger

from imbue.mngr.api.events import EventsTarget
from imbue.mngr.api.events import refresh_events_target
from imbue.mngr.api.events import try_build_events_target_for_agent
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_foreman.agent_registry import AgentRegistry
from imbue.mngr_foreman.connection_pool import ConnectionPool
from imbue.mngr_foreman.input_state import detect_blocking_dialog
from imbue.mngr_foreman.input_state import is_busy_state
from imbue.mngr_foreman.input_state import is_permissions_blocked
from imbue.mngr_foreman.interrupt import InterruptError
from imbue.mngr_foreman.interrupt import send_interrupt_to_agent
from imbue.mngr_foreman.messaging import MessageSendError
from imbue.mngr_foreman.messaging import send_message_to_agent
from imbue.mngr_foreman.terminal import handle_host_shell_ws
from imbue.mngr_foreman.terminal import handle_orchestrator_ws
from imbue.mngr_foreman.terminal import handle_terminal_ws
from imbue.mngr_foreman.transcript_images import externalize_event_images
from imbue.mngr_foreman.transcript_images import get_cached_image
from imbue.mngr_foreman.transcript_parser import parse_claude_session_lines
from imbue.mngr_foreman.transcript_tail import ReaderFn
from imbue.mngr_foreman.transcript_tail import SizeFn
from imbue.mngr_foreman.transcript_tail import TRANSCRIPT_SUBPATH
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
_TRANSCRIPT_POLL_SECONDS: Final[float] = 1.5
_TARGET_REFRESH_SECONDS: Final[float] = 30.0
_HEARTBEAT_SECONDS: Final[float] = 15.0


def _read_static(rel_path: str) -> tuple[bytes, str] | None:
    """Read a bundled static asset by relative path, or None if missing/escaping."""
    # Guard against path traversal: only allow simple forward-slashed names.
    if rel_path.startswith("/") or ".." in rel_path.split("/"):
        return None
    parts = [p for p in rel_path.split("/") if p]
    if not parts:
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


def create_app(
    mngr_ctx: MngrContext,
    registry: AgentRegistry,
    pool: ConnectionPool,
    max_tool_output_chars: int,
) -> Flask:
    # static_folder=None: foreman serves its own bundled assets via importlib
    # resources (see _read_static), so Flask's default /static route would only
    # add a duplicate rule pointing at a non-existent folder.
    app = Flask(__name__, static_folder=None)
    # Reject oversize uploads at the framework edge (a little headroom over the
    # 25MB file cap for multipart overhead); write_upload re-checks the raw bytes.
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 1024 * 1024
    sock = Sock(app)

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
        result = _read_static(rel_path)
        if result is None:
            return Response("Not found", status=404, mimetype="text/plain")
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
        return Response(data, mimetype=content_type.split(";")[0], content_type=content_type, headers=headers)

    # ---- agent list -------------------------------------------------------

    @app.route("/api/agents")
    def api_agents() -> Response:
        return jsonify({"agents": registry.snapshot()})

    @app.route("/api/agents/stream")
    def api_agents_stream() -> Response:
        def generate() -> Iterator[str]:
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
        if agent.type != "claude":
            return Response(
                _sse({"type": "unsupported", "agent_type": agent.type}),
                mimetype="text/event-stream",
                headers=_sse_headers(),
            )

        return Response(
            _transcript_stream(mngr_ctx, agent, max_tool_output_chars, pool),
            mimetype="text/event-stream",
            headers=_sse_headers(),
        )

    @app.route("/api/agents/<name>/timage/<image_id>")
    def api_transcript_image(name: str, image_id: str) -> ResponseReturnValue:
        # Serve a large transcript image that was externalized out of its SSE
        # frame. Bytes come from the in-memory cache keyed by the parser's id.
        if not image_id or any(c not in _SAFE_IMAGE_ID_CHARS for c in image_id):
            return Response("Not found", status=404, mimetype="text/plain")
        cached = get_cached_image(image_id)
        if cached is None:
            return Response("Not found", status=404, mimetype="text/plain")
        media_type, raw = cached
        return Response(
            raw, mimetype=media_type.split(";")[0], content_type=media_type, headers={"Cache-Control": "no-cache"}
        )

    # ---- send message -----------------------------------------------------

    @app.route("/api/agents/<name>/message", methods=["POST"])
    def api_message(name: str) -> ResponseReturnValue:
        payload = request.get_json(silent=True) or {}
        message = payload.get("message", "")
        if not isinstance(message, str) or not message.strip():
            return jsonify({"ok": False, "error": "Message is empty"}), 400
        try:
            send_message_to_agent(pool, name, message)
        except MessageSendError as e:
            # A blocking TUI dialog (permission / login) lands here -- surface it
            # so the UI can hint at the terminal page (phase 2).
            logger.info("Message to {} failed: {}", name, e)
            return jsonify({"ok": False, "error": str(e)}), 502
        return jsonify({"ok": True})

    @app.route("/api/agents/<name>/input-state")
    def api_input_state(name: str) -> ResponseReturnValue:
        # Cheap gate first: only a running claude agent can show a dialog. The
        # expensive tmux pane capture runs only past this gate.
        agent = registry.get_agent(name)
        if agent is None or agent.type != "claude":
            return jsonify({"blocked": False, "reason": None, "running": False, "busy": False, "state": None})
        state = str(agent.state.value if hasattr(agent.state, "value") else agent.state).upper()
        # ``busy`` is mngr's authoritative "claude is generating" signal (RUNNING);
        # the chat page uses it to clear a working dot the transcript tail misreads.
        busy = is_busy_state(state)
        if state not in ("RUNNING", "WAITING", "RUNNING_UNKNOWN_AGENT_TYPE"):
            return jsonify({"blocked": False, "reason": None, "running": False, "busy": False, "state": state})
        # BLOCKED beats busy in the UI: a mid-turn choice dialog can leave the
        # 'active' marker set (state RUNNING) while a menu is up, so a dialog must
        # win. mngr's own PERMISSIONS signal is a free, pane-less OR with the tmux
        # ❯ capture -- when it already says PERMISSIONS we skip the capture.
        if is_permissions_blocked(agent):
            reason: str | None = "permission prompt"
        else:
            reason = detect_blocking_dialog(pool, name)
        return jsonify(
            {"blocked": reason is not None, "reason": reason, "running": True, "busy": busy, "state": state}
        )

    @app.route("/api/agents/<name>/interrupt", methods=["POST"])
    def api_interrupt(name: str) -> ResponseReturnValue:
        # Send Escape to the agent's tmux pane (claude's "stop generating").
        try:
            send_interrupt_to_agent(mngr_ctx, name)
        except InterruptError as e:
            logger.info("Interrupt of {} failed: {}", name, e)
            return jsonify({"ok": False, "error": str(e)}), 502
        return jsonify({"ok": True})

    # ---- attachments ------------------------------------------------------

    @app.route("/api/agents/<name>/upload", methods=["POST"])
    def api_upload(name: str) -> ResponseReturnValue:
        # Multipart: the file plus the client-generated "<uuid>.<ext>" name. We
        # write it to <agent work_dir>/chat_uploads/ on the agent's host.
        if registry.get_agent(name) is None:
            return jsonify({"ok": False, "error": f"No agent named {name!r}"}), 404
        upload = request.files.get("file")
        stored_name = (request.form.get("filename") or "").strip()
        if upload is None or not stored_name:
            return jsonify({"ok": False, "error": "missing file or filename"}), 400
        try:
            path = write_upload(pool, name, stored_name, upload.read())
        except UploadError as e:
            logger.info("Upload to {} failed: {}", name, e)
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True, "path": path})

    @app.route("/api/agents/<name>/upload/<stored_name>", methods=["GET"])
    def api_get_upload(name: str, stored_name: str) -> ResponseReturnValue:
        # Serve an uploaded file's bytes back for inline rendering (image chips).
        if registry.get_agent(name) is None:
            return Response("Not found", status=404, mimetype="text/plain")
        try:
            data = read_upload(pool, name, stored_name)
        except UploadNotFound:
            return Response("Not found", status=404, mimetype="text/plain")
        except UploadError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return Response(
            data,
            mimetype=content_type_for_name(stored_name).split(";")[0],
            content_type=content_type_for_name(stored_name),
            headers={"Cache-Control": "no-cache"},
        )

    @app.route("/api/agents/<name>/upload/<stored_name>", methods=["DELETE"])
    def api_delete_upload(name: str, stored_name: str) -> ResponseReturnValue:
        try:
            delete_upload(pool, name, stored_name)
        except UploadError as e:
            logger.info("Upload delete for {} failed: {}", name, e)
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True})

    # ---- terminal websocket ----------------------------------------------

    @sock.route("/ws/agents/<name>/terminal")
    def terminal_ws(ws: object, name: str) -> None:
        # Bridge the socket to the agent's tmux (direct ssh, mngr-connect fallback).
        handle_terminal_ws(ws, name, pool)

    @sock.route("/ws/hosts/<host>/terminal")
    def host_shell_ws(ws: object, host: str) -> None:
        # A plain login shell on a known host (resolved via any agent on it).
        # Resolve to any agent on this host; handle_host_shell_ws closes the ws
        # itself if the host can't be reached, so a missing agent is the only case
        # we short-circuit here (pass a sentinel the handler treats as unavailable).
        agent_name = _first_agent_on_host(host) or ""
        handle_host_shell_ws(ws, agent_name, host, pool)

    @sock.route("/ws/terminal")
    def orchestrator_ws(ws: object) -> None:
        # Bridge the socket to a plain `bash -l` on the foreman server machine.
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


def _build_transcript_reader(
    mngr_ctx: MngrContext, agent: AgentDetails, pool: ConnectionPool
) -> tuple[ReaderFn, SizeFn] | None:
    """Build ``(reader, size_fn)`` over the agent's mirrored transcript file.

    ``reader() -> bytes`` reads the raw-transcript file beneath the resolved
    ``EventsTarget``'s ``events_path`` parent; ``size_fn() -> int | None`` cheaply
    stats its byte size over the warm connection pool so the poll can skip the read
    when nothing changed. The target is refreshed periodically so both survive a
    host stop/start.
    """
    initial_target = try_build_events_target_for_agent(
        mngr_ctx=mngr_ctx,
        agent_id=agent.id,
        agent_name=str(agent.name),
        host_id=agent.host.id,
        provider_name=agent.host.provider_name,
    )
    if initial_target is None:
        return None

    target: EventsTarget = initial_target
    refreshed_at: float = time.monotonic()
    agent_name = str(agent.name)

    def _transcript_path(t: EventsTarget) -> Path:
        assert t.events_path is not None
        return t.events_path.parent / TRANSCRIPT_SUBPATH

    def reader() -> bytes:
        nonlocal target, refreshed_at
        now = time.monotonic()
        if now - refreshed_at >= _TARGET_REFRESH_SECONDS:
            try:
                target = refresh_events_target(target)
            except Exception as e:  # noqa: BLE001 - keep last good target
                logger.trace("refresh_events_target failed (keeping previous): {}", e)
            refreshed_at = now
        if target.host is None or target.events_path is None:
            return b""
        return target.host.read_file(_transcript_path(target))

    def size_fn() -> int | None:
        if target.events_path is None:
            return None
        path = _transcript_path(target)

        def _stat(_a: AgentInterface, host: OnlineHostInterface) -> int | None:
            result = host.execute_stateful_command(f"stat -c %s {shlex.quote(str(path))} 2>/dev/null")
            out = (result.stdout or "").strip()
            return int(out) if result.success and out.isdigit() else None

        try:
            return pool.run_on_host(agent_name, _stat)
        except Exception:  # noqa: BLE001 - a failed stat just means "read anyway"
            return None

    return reader, size_fn


def _transcript_stream(
    mngr_ctx: MngrContext,
    agent: AgentDetails,
    max_tool_output_chars: int,
    pool: ConnectionPool,
) -> Iterator[str]:
    """SSE generator: full backfill, then live events, with periodic heartbeats."""
    built = _build_transcript_reader(mngr_ctx, agent, pool)
    if built is None:
        yield _sse({"type": "error", "message": "Agent host is not readable (offline and no volume)."})
        return
    reader, size_fn = built

    tailer = TranscriptTailer(reader, size_fn=size_fn)
    existing_event_ids: set[str] = set()
    tool_name_by_call_id: dict[str, str] = {}

    def _emit(lines: list[str]) -> Iterator[str]:
        if not lines:
            return
        events = parse_claude_session_lines(
            lines,
            existing_event_ids=existing_event_ids,
            tool_name_by_call_id=tool_name_by_call_id,
            max_tool_output_chars=max_tool_output_chars,
        )
        for event in events:
            # Move large base64 images out-of-band so SSE frames stay small; the
            # client fetches them by id from the /timage endpoint.
            externalize_event_images(event)
            yield _sse({"type": "event", "event": event})

    # Backfill: first poll reads the whole file from offset 0.
    try:
        backfill_lines = tailer.poll()
    except Exception as e:  # noqa: BLE001
        yield _sse({"type": "error", "message": f"Failed to read transcript: {e}"})
        return
    yield from _emit(backfill_lines)
    yield _sse({"type": "backfill_complete"})

    # Live follow.
    last_heartbeat = time.monotonic()
    while True:
        time.sleep(_TRANSCRIPT_POLL_SECONDS)
        try:
            new_lines = tailer.poll()
        except Exception as e:  # noqa: BLE001
            logger.trace("Transcript poll error (continuing): {}", e)
            new_lines = []
        yield from _emit(new_lines)
        now = time.monotonic()
        if now - last_heartbeat >= _HEARTBEAT_SECONDS:
            yield ": heartbeat\n\n"
            last_heartbeat = now


def run_server(
    mngr_ctx: MngrContext,
    host: str,
    port: int,
    max_tool_output_chars: int,
) -> None:
    """Start the registry + warm connection pool and serve forever (blocking)."""
    registry = AgentRegistry(mngr_ctx)
    registry.start()
    pool = ConnectionPool(mngr_ctx)
    pool.start_maintainer(registry)
    app = create_app(mngr_ctx, registry, pool, max_tool_output_chars)
    # threaded=True so SSE connections (one long-lived generator each) don't
    # block the list/message endpoints; use_reloader=False (single process).
    app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
