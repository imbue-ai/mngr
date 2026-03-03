import shlex
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final
from typing import IO

from loguru import logger
from pydantic import Field
from pydantic import model_validator

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mng.api.connect import build_ssh_base_args
from imbue.mng.api.find import resolve_agent_reference
from imbue.mng.api.find import resolve_host_reference
from imbue.mng.api.list import load_all_agents_grouped_by_host
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.errors import UserInputError
from imbue.mng.interfaces.data_types import VolumeFileType
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.interfaces.volume import Volume
from imbue.mng.primitives import HostId
from imbue.mng.providers.base_provider import BaseProviderInstance
from imbue.mng.utils.interactive_subprocess import popen_interactive_subprocess
from imbue.mng.utils.polling import run_periodically

FOLLOW_POLL_INTERVAL_SECONDS: Final[float] = 1.0


class EventsTarget(FrozenModel):
    """Resolved target for the events command."""

    volume: Volume | None = Field(default=None, description="Volume scoped to the target's events directory")
    online_host: OnlineHostInterface | None = Field(
        default=None, description="Online host for direct command execution"
    )
    events_path: Path | None = Field(default=None, description="Absolute path to the events directory on the host")
    display_name: str = Field(description="Human-readable name for the target (agent or host)")

    model_config = {"arbitrary_types_allowed": True}

    @model_validator(mode="after")
    def _validate_online_host_and_events_path_are_paired(self) -> "EventsTarget":
        """Ensure online_host and events_path are either both set or both None."""
        is_host_set = self.online_host is not None
        is_path_set = self.events_path is not None
        if is_host_set != is_path_set:
            raise MngError("online_host and events_path must both be set or both be None")
        return self


class EventFileEntry(FrozenModel):
    """Information about an available event file."""

    name: str = Field(description="Event file name")
    size: int = Field(description="File size in bytes")


def resolve_events_target(
    identifier: str,
    mng_ctx: MngContext,
) -> EventsTarget:
    """Resolve a target identifier (agent or host name/ID) to an EventsTarget.

    First tries to find an agent with the given identifier.
    If no agent is found, tries to find a host.
    Uses resolve_agent_reference and resolve_host_reference from api/find.py.

    When the target host is online, the returned EventsTarget includes the
    online host and events path for direct command execution (e.g., tail -f).
    """
    with log_span("Loading agents and hosts"):
        agents_by_host, _providers = load_all_agents_grouped_by_host(mng_ctx, include_destroyed=False)

    all_hosts = list(agents_by_host.keys())

    # Try finding as an agent first
    # Only suppress "not found" errors; re-raise ambiguity ("Multiple") errors
    try:
        agent_result = resolve_agent_reference(identifier, None, agents_by_host)
    except UserInputError as e:
        if "Multiple" in str(e):
            raise
        logger.trace("Agent lookup did not find {}: {}", identifier, e)
        agent_result = None

    if agent_result is not None:
        host_ref, agent_ref = agent_result
        with log_span("Getting events access for agent {}", agent_ref.agent_name):
            provider = get_provider_instance(host_ref.provider_name, mng_ctx)

            # Try to get the volume
            host_volume = provider.get_volume_for_host(host_ref.host_id)
            events_volume: Volume | None = None
            if host_volume is not None:
                agent_volume = host_volume.get_agent_volume(agent_ref.agent_id)
                events_volume = agent_volume.scoped("events")

            # Try to get the online host for direct access
            online_host, events_path = _try_get_online_host_for_events(
                provider, host_ref.host_id, Path("agents") / str(agent_ref.agent_id) / "events"
            )

            if events_volume is None and online_host is None:
                raise MngError(
                    f"Provider '{host_ref.provider_name}' does not support volumes and the host is not online. "
                    "Cannot read events for this agent."
                )

        return EventsTarget(
            volume=events_volume,
            online_host=online_host,
            events_path=events_path,
            display_name=f"agent '{agent_ref.agent_name}'",
        )

    # Try finding as a host
    # Only suppress "not found" errors; re-raise ambiguity ("Multiple") errors
    try:
        host_ref = resolve_host_reference(identifier, all_hosts)
    except UserInputError as e:
        if "Multiple" in str(e):
            raise
        logger.trace("Host lookup did not find {}: {}", identifier, e)
        host_ref = None

    if host_ref is not None:
        with log_span("Getting events access for host {}", host_ref.host_name):
            provider = get_provider_instance(host_ref.provider_name, mng_ctx)

            # Try to get the volume
            host_volume = provider.get_volume_for_host(host_ref.host_id)
            events_volume = None
            if host_volume is not None:
                events_volume = host_volume.volume.scoped("events")

            # Try to get the online host for direct access
            online_host, events_path = _try_get_online_host_for_events(provider, host_ref.host_id, Path("events"))

            if events_volume is None and online_host is None:
                raise MngError(
                    f"Provider '{host_ref.provider_name}' does not support volumes and the host is not online. "
                    "Cannot read events for this host."
                )

        return EventsTarget(
            volume=events_volume,
            online_host=online_host,
            events_path=events_path,
            display_name=f"host '{host_ref.host_name}'",
        )

    raise UserInputError(f"No agent or host found with name or ID: {identifier}")


def _try_get_online_host_for_events(
    provider: BaseProviderInstance,
    host_id: HostId,
    events_subpath: Path,
) -> tuple[OnlineHostInterface | None, Path | None]:
    """Try to get the online host and compute the absolute events path.

    Returns (online_host, events_path) if the host is online, (None, None) otherwise.
    """
    try:
        host_interface = provider.get_host(host_id)
    except MngError as e:
        logger.trace("Host {} is not available for direct event access: {}", host_id, e)
        return None, None

    if not isinstance(host_interface, OnlineHostInterface):
        return None, None

    events_path = host_interface.host_dir / str(events_subpath)
    return host_interface, events_path


# =============================================================================
# List event files
# =============================================================================


def list_event_files(target: EventsTarget) -> list[EventFileEntry]:
    """List available event files in the target's events directory."""
    # Prefer host-based listing (direct access to the online host)
    if target.online_host is not None and target.events_path is not None:
        return _list_event_files_via_host(target.online_host, target.events_path, target.display_name)

    # Fall back to volume-based listing
    if target.volume is not None:
        with log_span("Listing event files for {} via volume", target.display_name):
            entries = target.volume.listdir("")
            return [
                EventFileEntry(name=_extract_filename(entry.path), size=entry.size)
                for entry in entries
                if entry.file_type == VolumeFileType.FILE
            ]

    raise MngError(f"Cannot list event files for {target.display_name}: no volume or online host available")


def _list_event_files_via_host(
    online_host: OnlineHostInterface,
    events_path: Path,
    display_name: str,
) -> list[EventFileEntry]:
    """List event files by executing a command on the online host."""
    with log_span("Listing event files for {} via host", display_name):
        # Use a shell loop with stat to get file names and sizes.
        # Uses GNU stat with macOS fallback (stat -c %s vs stat -f %z).
        # The trailing "true" ensures exit code 0 regardless of the last [ -f ] test.
        cmd = (
            f"cd {shlex.quote(str(events_path))} 2>/dev/null && "
            f"for f in *; do "
            f'[ -f "$f" ] && SIZE=$(stat -c %s "$f" 2>/dev/null || stat -f %z "$f" 2>/dev/null) '
            f'&& printf "%s\\t%s\\n" "$f" "$SIZE"; '
            f"done; true"
        )
        result = online_host.execute_command(cmd, timeout_seconds=10.0)
        if not result.stdout.strip():
            return []

        return _parse_file_listing_output(result.stdout)


@pure
def _parse_file_listing_output(output: str) -> list[EventFileEntry]:
    """Parse tab-separated name/size output into EventFileEntry objects."""
    entries: list[EventFileEntry] = []
    for line in output.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            name = parts[0]
            try:
                size = int(parts[1])
            except ValueError:
                logger.trace("Could not parse file size for '{}': '{}'", name, parts[1])
                size = 0
            entries.append(EventFileEntry(name=name, size=size))
    return entries


@pure
def _extract_filename(path: str) -> str:
    """Extract the filename from a volume path."""
    return path.rsplit("/", 1)[-1] if "/" in path else path


# =============================================================================
# Read event content
# =============================================================================


def read_event_content(target: EventsTarget, event_file_name: str) -> str:
    """Read the full content of an event file."""
    # Prefer host-based reading (direct access to the online host)
    if target.online_host is not None and target.events_path is not None:
        return _read_event_content_via_host(
            target.online_host, target.events_path, event_file_name, target.display_name
        )

    # Fall back to volume-based reading
    if target.volume is not None:
        with log_span("Reading event file '{}' for {} via volume", event_file_name, target.display_name):
            content_bytes = target.volume.read_file(event_file_name)
            return content_bytes.decode("utf-8", errors="replace")

    raise MngError(f"Cannot read event file for {target.display_name}: no volume or online host available")


def _read_event_content_via_host(
    online_host: OnlineHostInterface,
    events_path: Path,
    event_file_name: str,
    display_name: str,
) -> str:
    """Read event content by executing cat on the online host."""
    with log_span("Reading event file '{}' for {} via host", event_file_name, display_name):
        file_path = events_path / event_file_name
        result = online_host.execute_command(
            f"cat {shlex.quote(str(file_path))}",
            timeout_seconds=30.0,
        )
        if not result.success:
            raise MngError(f"Failed to read event file '{event_file_name}': {result.stderr}")
        return result.stdout


# =============================================================================
# Head/tail filtering
# =============================================================================


@pure
def apply_head_or_tail(
    content: str,
    head_count: int | None,
    tail_count: int | None,
) -> str:
    """Apply head or tail line filtering to content."""
    if head_count is None and tail_count is None:
        return content
    lines = content.splitlines(keepends=True)
    if head_count is not None:
        lines = lines[:head_count]
    else:
        # tail_count is guaranteed non-None here (early return above handles both-None case)
        assert tail_count is not None
        lines = lines[-tail_count:]
    return "".join(lines)


# =============================================================================
# Follow event file
# =============================================================================


class _FollowState(MutableModel):
    """Mutable state for the follow polling loop."""

    previous_length: int = Field(description="Length of content at last check")


def _check_for_new_content(
    target: EventsTarget,
    event_file_name: str,
    on_new_content: Callable[[str], None],
    state: _FollowState,
) -> bool:
    """Check for new content and emit it. Always returns False to keep polling."""
    try:
        current_content = read_event_content(target, event_file_name)
    except (MngError, OSError) as e:
        logger.trace("Failed to read event file during follow: {}", e)
        return False
    current_length = len(current_content)
    if current_length > state.previous_length:
        new_content = current_content[state.previous_length :]
        on_new_content(new_content)
        state.previous_length = current_length
    elif current_length < state.previous_length:
        # File was truncated, re-read from the start
        logger.debug("Event file was truncated, re-reading from start")
        on_new_content(current_content)
        state.previous_length = current_length
    else:
        pass
    return False


def follow_event_file(
    target: EventsTarget,
    event_file_name: str,
    # Callback invoked with new content each time the file changes
    on_new_content: Callable[[str], None],
    tail_count: int | None,
) -> None:
    """Follow an event file, streaming new content as it appears.

    When the target has an online host, uses tail -f for real-time streaming
    (locally or via SSH). Otherwise falls back to volume-based polling.
    """
    # Prefer host-based tail -f for real-time streaming
    if target.online_host is not None and target.events_path is not None:
        _follow_event_file_via_host(
            target.online_host,
            target.events_path / event_file_name,
            on_new_content,
            tail_count,
        )
        return

    # Fall back to volume-based polling
    if target.volume is not None:
        _follow_event_file_via_volume(target, event_file_name, on_new_content, tail_count)
        return

    raise MngError(f"Cannot follow event file for {target.display_name}: no volume or online host available")


def _follow_event_file_via_volume(
    target: EventsTarget,
    event_file_name: str,
    on_new_content: Callable[[str], None],
    tail_count: int | None,
) -> None:
    """Follow an event file using volume-based polling."""
    assert target.volume is not None

    # Read initial content
    try:
        content = read_event_content(target, event_file_name)
    except (MngError, OSError) as e:
        logger.debug("Failed to read initial event content: {}", e)
        content = ""

    # Show initial content (with optional tail)
    initial_content = apply_head_or_tail(content, head_count=None, tail_count=tail_count)
    if initial_content:
        on_new_content(initial_content)

    state = _FollowState(previous_length=len(content))

    # Run indefinitely until interrupted (KeyboardInterrupt propagates out)
    run_periodically(
        fn=lambda: _check_for_new_content(target, event_file_name, on_new_content, state),
        interval=FOLLOW_POLL_INTERVAL_SECONDS,
    )


def _follow_event_file_via_host(
    online_host: OnlineHostInterface,
    event_file_path: Path,
    on_new_content: Callable[[str], None],
    tail_count: int | None,
) -> None:
    """Follow an event file using tail -f on the host (locally or via SSH).

    For local hosts, runs tail -f directly as a subprocess.
    For remote hosts, runs tail -f via SSH for real-time streaming.
    """
    tail_args = _build_tail_args(event_file_path, tail_count)

    if online_host.is_local:
        # Local host: run tail directly
        cmd = tail_args
    else:
        # Remote host: wrap in SSH
        tail_cmd_str = " ".join(shlex.quote(a) for a in tail_args)
        ssh_args = build_ssh_base_args(online_host)
        cmd = ssh_args + [tail_cmd_str]

    logger.debug("Following event file via host: {}", " ".join(cmd))

    process = popen_interactive_subprocess(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert process.stderr is not None

        # Drain stderr in a background thread to prevent pipe buffer deadlock
        stderr_chunks: list[bytes] = []
        stderr_thread = threading.Thread(target=_drain_pipe, args=(process.stderr, stderr_chunks), daemon=True)
        stderr_thread.start()

        # Stream stdout line by line
        for raw_line in iter(process.stdout.readline, b""):
            on_new_content(raw_line.decode("utf-8", errors="replace"))

        # The stdout loop ended because the process exited; check for errors
        process.wait()
        stderr_thread.join(timeout=5)
        if process.returncode != 0:
            stderr_output = b"".join(stderr_chunks).decode("utf-8", errors="replace")
            raise MngError(f"Failed to follow event file (exit code {process.returncode}): {stderr_output.strip()}")
    except KeyboardInterrupt:
        raise
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _drain_pipe(pipe: IO[bytes], chunks: list[bytes]) -> None:
    """Read all data from a pipe and append to chunks. Used as a thread target."""
    chunks.append(pipe.read())


@pure
def _build_tail_args(event_file_path: Path, tail_count: int | None) -> list[str]:
    """Build the command-line args for tail -f."""
    args = ["tail"]
    if tail_count is not None:
        args.extend(["-n", str(tail_count)])
    else:
        # Show entire file then follow (equivalent to cat + tail -f)
        args.extend(["-n", "+1"])
    args.extend(["-f", str(event_file_path)])
    return args
