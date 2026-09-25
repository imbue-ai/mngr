"""Shared utilities for single-command listing data collection.

Providers that run agents on remote hosts can use these helpers to collect
all listing data (host status, agent status, activity timestamps, etc.)
in a single SSH command instead of making many individual round-trips.

The shell script collects structured output with unique delimiters, and
the parser extracts it into a dict suitable for building HostDetails and
AgentDetails.

There are two variants:
- ``build_listing_collection_script`` runs *inside* the host (filesystem
  paths are real). Used by providers that have direct SSH access to the
  host (or run via ``docker exec`` into a running container).
- ``build_outer_listing_collection_script`` runs on an outer/VPS root
  shell that has ``docker`` available. It looks up the container by
  label, dispatches to ``docker exec`` for running containers, or to
  ``docker cp`` + a stopped-variant script for non-running ones. This
  lets us collect listing data without needing the inner container's
  sshd to be reachable -- a stopped container still surfaces its
  ``data.json``, host name, agents, etc.
"""

import json
import shlex
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure

# Unique delimiters for parsing the single-command output
SEP_DATA_JSON_START: Final[str] = "---MNGR_DATA_JSON_START---"
SEP_DATA_JSON_END: Final[str] = "---MNGR_DATA_JSON_END---"
SEP_AGENT_START: Final[str] = "---MNGR_AGENT_START:"
SEP_AGENT_END: Final[str] = "---MNGR_AGENT_END---"
SEP_AGENT_DATA_START: Final[str] = "---MNGR_AGENT_DATA_START---"
SEP_AGENT_DATA_END: Final[str] = "---MNGR_AGENT_DATA_END---"
SEP_PS_START: Final[str] = "---MNGR_PS_START---"
SEP_PS_END: Final[str] = "---MNGR_PS_END---"
SEP_AGENT_MTIMES_START: Final[str] = "---MNGR_AGENT_MTIMES_START---"
SEP_AGENT_MTIMES_END: Final[str] = "---MNGR_AGENT_MTIMES_END---"
SEP_TMUX_PANES_START: Final[str] = "---MNGR_TMUX_PANES_START---"
SEP_TMUX_PANES_END: Final[str] = "---MNGR_TMUX_PANES_END---"

# Separates a tmux pane line's session and window names from its lifecycle fields.
_TMUX_PANE_FIELD_SEPARATOR: Final[str] = "::MNGR::"

# The activity files whose mtimes the listing reports, by the agent-dict key each fills.
_AGENT_ACTIVITY_MTIME_KEY_BY_FILE_NAME: Final[dict[str, str]] = {
    "user": "user_activity_mtime",
    "agent": "agent_activity_mtime",
    "start": "start_activity_mtime",
}


@pure
def _build_host_dir_resolution_script(host_dir: str, fallback_host_dirs: Sequence[str]) -> str:
    """Build the prelude that picks the host_dir this host actually uses.

    A host keeps the host_dir it was baked with for life, so the provider config
    resolved in the current context can name a directory that host has never
    had. Probing the candidates in order -- configured first, then the rest --
    lets one client read hosts of either generation.

    The probe is ``data.json``, not directory existence: a failed read against
    the wrong candidate can *create* that directory as an empty husk (mngr
    mkdir -p's the state dir on write paths), so existence proves nothing while
    ``data.json`` is exactly the certified data the caller came for. With no
    candidate matching, the configured value is used unchanged, which is what a
    host mid-bootstrap (no data.json yet) needs.
    """
    candidates = " ".join(shlex.quote(candidate) for candidate in (host_dir, *fallback_host_dirs))
    return f"""
HOST_DIR={shlex.quote(host_dir)}
for _mngr_candidate in {candidates}; do
    if [ -f "$_mngr_candidate/data.json" ]; then
        HOST_DIR="$_mngr_candidate"
        break
    fi
done
echo "HOST_DIR=$HOST_DIR"
"""


@pure
def build_listing_collection_script(
    host_dir: str,
    prefix: str,
    window_name: str = "agent",
    fallback_host_dirs: Sequence[str] = (),
) -> str:
    """Build a shell script that collects all listing data in one command.

    ``window_name`` is the name of the agent's primary tmux window (config
    ``tmux.primary_window_name``); lifecycle detection targets that window by
    name so it works regardless of the user's tmux ``base-index``.

    ``fallback_host_dirs`` are other host_dir locations to fall back to when
    ``host_dir`` holds no ``data.json`` (see
    :func:`_build_host_dir_resolution_script`; callers reading a mngr-baked
    container pass :func:`~imbue.mngr.providers.host_dir_layouts.host_dir_fallbacks`).
    The resolved directory is echoed as ``HOST_DIR=`` so the caller can record it
    per host. Defaults to empty: a provider whose hosts only ever use its
    configured host_dir keeps today's single-candidate behavior.
    """
    tmux_pane_format = _TMUX_PANE_FIELD_SEPARATOR.join(
        ("#{session_name}", "#{window_name}", "#{pane_dead}|#{pane_current_command}|#{pane_pid}")
    )
    return f"""
{_build_host_dir_resolution_script(host_dir, fallback_host_dirs)}
echo {shlex.quote(f"TMUX_SESSION_PREFIX={prefix}")}
echo {shlex.quote(f"TMUX_WINDOW_NAME={window_name}")}

# Uptime
echo "UPTIME=$(cat /proc/uptime 2>/dev/null | awk '{{print $1}}')"

# Boot time
echo "BTIME=$(grep '^btime ' /proc/stat 2>/dev/null | awk '{{print $2}}')"

# Host lock: held-state (a real flock, probed non-blockingly) and mtime (for
# display). The lock file persists after release, so existence != held; guard on
# existence so the probe never creates it.
echo "LOCK_HELD=$([ -e "$HOST_DIR/host_lock" ] && ! flock -n "$HOST_DIR/host_lock" -c true 2>/dev/null && echo true || echo false)"
echo "LOCK_MTIME=$(stat -c %Y "$HOST_DIR/host_lock" 2>/dev/null)"

# SSH activity mtime
echo "SSH_ACTIVITY_MTIME=$(stat -c %Y "$HOST_DIR/activity/ssh" 2>/dev/null)"

# Host data.json
echo '{SEP_DATA_JSON_START}'
cat "$HOST_DIR/data.json" 2>/dev/null || echo '{{}}'
echo ''
echo '{SEP_DATA_JSON_END}'

# ps output (shared by all agents for lifecycle detection)
echo '{SEP_PS_START}'
ps -e -o pid=,ppid=,comm= 2>/dev/null
echo '{SEP_PS_END}'

# Every pane on the server, joined to its agent by session and window name on the parsing side
echo '{SEP_TMUX_PANES_START}'
if [ -d "$HOST_DIR/agents" ]; then
    tmux list-panes -a -F {shlex.quote(tmux_pane_format)} 2>/dev/null
fi
echo '{SEP_TMUX_PANES_END}'
{_build_agents_section_script(is_host_running=True)}
"""


@pure
def _build_agents_section_script(is_host_running: bool) -> str:
    """Build the script section that emits every agent under ``$HOST_DIR/agents``.

    Everything that can be collected for all agents at once is: one ``stat`` covers
    every activity file, and the caller's tmux listing is one command. The per-agent
    loop is left with its ``cat`` of the data.json (and a ``tr`` of the URL file for an
    agent that has one), because on a sandboxed runtime (gVisor) each process spawn
    costs tens of milliseconds and a host can carry dozens of agents. A stopped host
    has no running agent, so its leftover ``active`` markers are not read.
    """
    active_check = (
        """if [ -f "${agent_dir}active" ]; then
            echo "ACTIVE=true"
        else
            echo "ACTIVE=false"
        fi"""
        if is_host_running
        else 'echo "ACTIVE=false"'
    )
    return f"""
echo '{SEP_AGENT_MTIMES_START}'
# GNU stat exits non-zero when any one path is missing, so the flavor is probed once rather
# than falling back on failure (BSD's ``-f`` means something else to GNU stat).
if stat -c %Y / >/dev/null 2>&1; then
    stat -c '%Y %n' "$HOST_DIR"/agents/*/activity/user "$HOST_DIR"/agents/*/activity/agent "$HOST_DIR"/agents/*/activity/start 2>/dev/null
else
    stat -f '%m %N' "$HOST_DIR"/agents/*/activity/user "$HOST_DIR"/agents/*/activity/agent "$HOST_DIR"/agents/*/activity/start 2>/dev/null
fi
echo '{SEP_AGENT_MTIMES_END}'
if [ -d "$HOST_DIR/agents" ]; then
    for agent_dir in "$HOST_DIR/agents"/*/; do
        [ -d "$agent_dir" ] || continue
        data_file="${{agent_dir}}data.json"
        [ -f "$data_file" ] || continue
        agent_id="${{agent_dir%/}}"
        agent_id="${{agent_id##*/}}"
        echo '{SEP_AGENT_START}'"$agent_id"'---'
        echo '{SEP_AGENT_DATA_START}'
        cat "$data_file"
        echo ''
        echo '{SEP_AGENT_DATA_END}'
        {active_check}
        if [ -f "${{agent_dir}}status/url" ]; then
            echo "URL=$(tr -d '\\n' < "${{agent_dir}}status/url")"
        else
            echo "URL="
        fi
        echo '{SEP_AGENT_END}'
    done
fi
"""


@pure
def _build_stopped_listing_collection_script(prefix: str) -> str:
    """Build a script that reads listing data from an *extracted* host_dir tree.

    Used in the stopped-container branch of ``build_outer_listing_collection_script``
    after ``docker cp`` has copied the container's host_dir to a temp path on
    the outer host. Expects ``HOST_DIR`` env var to point at that path. Emits
    the same delimiter format as ``build_listing_collection_script`` so the
    same parser handles both. Skips fields that only make sense for a running
    container (uptime, btime, ps output, tmux info, active marker).
    """
    return f"""
# A stopped container has no running process, so the lock cannot be held.
echo "LOCK_HELD=false"
echo "LOCK_MTIME=$(stat -c %Y "$HOST_DIR/host_lock" 2>/dev/null)"
echo "SSH_ACTIVITY_MTIME=$(stat -c %Y "$HOST_DIR/activity/ssh" 2>/dev/null)"
echo '{SEP_DATA_JSON_START}'
cat "$HOST_DIR/data.json" 2>/dev/null || echo '{{}}'
echo ''
echo '{SEP_DATA_JSON_END}'
echo '{SEP_PS_START}'
echo '{SEP_PS_END}'
{_build_agents_section_script(is_host_running=False)}
"""


# Unique heredoc terminators so the embedded inner scripts can't accidentally
# collide with a line of bash inside their own content.
_INNER_RUNNING_EOF: Final[str] = "MNGR_INNER_LISTING_EOF_a7f3d9e2"
_INNER_STOPPED_EOF: Final[str] = "MNGR_STOPPED_LISTING_EOF_a7f3d9e2"


@pure
def build_outer_listing_collection_script(
    host_id: str,
    host_dir: str,
    prefix: str,
    host_id_label: str = "com.imbue.mngr.host-id",
    window_name: str = "agent",
    fallback_host_dirs: Sequence[str] = (),
) -> str:
    """Build a script that runs on the outer (VPS root) and collects listing data.

    Looks up the container by ``<host_id_label>=<host_id>`` label, then:
    - if the container is missing: emits ``CONTAINER_MISSING=true``.
    - if the container is running: ``docker exec``s the inner listing script.
    - otherwise: ``docker cp``s the host_dir tree to a temp path on the outer
      host and runs the stopped-variant listing script against it.

    Always prepends ``CONTAINER_STATE=`` and ``CONTAINER_EXIT_CODE=`` lines so
    the caller can map the docker container status to a ``HostState`` without
    a second round-trip.

    ``fallback_host_dirs`` are other host_dir locations to try when ``host_dir``
    holds no ``data.json``; both branches emit the ``HOST_DIR=`` they settled on,
    always as a path *inside the container* (the stopped branch reports the
    source of the copy, not the outer temp path it was extracted to).
    """
    inner_running = build_listing_collection_script(host_dir, prefix, window_name, fallback_host_dirs)
    inner_stopped = _build_stopped_listing_collection_script(prefix)
    candidate_host_dirs = " ".join(shlex.quote(candidate) for candidate in (host_dir, *fallback_host_dirs))
    quoted_host_id = shlex.quote(str(host_id))
    quoted_host_dir = shlex.quote(host_dir)
    quoted_label = shlex.quote(host_id_label)
    return f"""CID=$(docker ps -aq --filter label={quoted_label}={quoted_host_id} | head -1)
if [ -z "$CID" ]; then
    echo "CONTAINER_MISSING=true"
    exit 0
fi
STATE=$(docker inspect --format '{{{{.State.Status}}}}' "$CID" 2>/dev/null)
EXIT_CODE=$(docker inspect --format '{{{{.State.ExitCode}}}}' "$CID" 2>/dev/null)
echo "CONTAINER_STATE=$STATE"
echo "CONTAINER_EXIT_CODE=$EXIT_CODE"
if [ "$STATE" = "running" ]; then
    # ``-w /`` overrides the container's cwd (which can refer to a path
    # that no longer exists in the container filesystem, causing
    # ``OCI runtime exec failed: chdir to cwd ... no such file or directory``)
    docker exec -i -w / "$CID" bash <<'{_INNER_RUNNING_EOF}'
{inner_running}
{_INNER_RUNNING_EOF}
    exit 0
fi
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
# A stopped container cannot be exec'd into, so the candidates are tried by
# copying each out in turn and keeping the first that carries a data.json.
# ``docker cp`` of an absent path fails outright, so the loop doubles as the
# existence probe the running branch does with a stat.
EXTRACTED=
RESOLVED_HOST_DIR={quoted_host_dir}
for _mngr_candidate in {candidate_host_dirs}; do
    _mngr_dest="$TMP/extract-$(echo "$_mngr_candidate" | tr -c 'A-Za-z0-9' '_')"
    mkdir -p "$_mngr_dest"
    docker cp "$CID":"$_mngr_candidate" "$_mngr_dest/" 2>/dev/null || continue
    _mngr_extracted="$_mngr_dest/$(basename "$_mngr_candidate")"
    [ -d "$_mngr_extracted" ] || continue
    if [ -z "$EXTRACTED" ]; then
        # Remember the first readable candidate, so a set of candidates that
        # all lack a data.json still reports the same tree today's single-path
        # extraction would have.
        EXTRACTED="$_mngr_extracted"
        RESOLVED_HOST_DIR="$_mngr_candidate"
    fi
    if [ -f "$_mngr_extracted/data.json" ]; then
        EXTRACTED="$_mngr_extracted"
        RESOLVED_HOST_DIR="$_mngr_candidate"
        break
    fi
done
if [ -z "$EXTRACTED" ]; then
    echo "EXTRACTION_FAILED=true"
    exit 0
fi
echo "HOST_DIR=$RESOLVED_HOST_DIR"
HOST_DIR="$EXTRACTED" bash <<'{_INNER_STOPPED_EOF}'
{inner_stopped}
{_INNER_STOPPED_EOF}
"""


@pure
def parse_optional_int(value: str) -> int | None:
    """Parse an optional integer from a key=value line's value portion."""
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError:
        return None


@pure
def parse_optional_float(value: str) -> float | None:
    """Parse an optional float from a key=value line's value portion."""
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return float(stripped)
    except ValueError:
        return None


def _extract_delimited_block(lines: list[str], idx: int, end_marker: str) -> tuple[str, int]:
    """Extract lines between the current position and end_marker, returning the content and new index."""
    collected: list[str] = []
    while idx < len(lines) and lines[idx].strip() != end_marker:
        collected.append(lines[idx])
        idx += 1
    return "\n".join(collected).strip(), idx


def _parse_agent_section(lines: list[str], idx: int) -> tuple[dict[str, Any], int]:
    """Parse a single agent section, returning the agent dict and new index."""
    agent_raw: dict[str, Any] = {}

    while idx < len(lines) and lines[idx].strip() != SEP_AGENT_END:
        aline = lines[idx]
        if aline.strip() == SEP_AGENT_DATA_START:
            idx += 1
            agent_json_str, idx = _extract_delimited_block(lines, idx, SEP_AGENT_DATA_END)
            if agent_json_str:
                try:
                    agent_raw["data"] = json.loads(agent_json_str)
                except json.JSONDecodeError as e:
                    logger.warning("Failed to parse agent data.json in listing output: {}", e)
        elif aline.startswith("ACTIVE="):
            agent_raw["is_active"] = aline[len("ACTIVE=") :].strip() == "true"
        elif aline.startswith("URL="):
            val = aline[len("URL=") :].strip()
            agent_raw["url"] = val if val else None
        else:
            pass
        idx += 1

    return agent_raw, idx


@pure
def _parse_agent_activity_mtimes(block: str) -> dict[tuple[str, str], int]:
    """Each ``(agent dir name, activity file name)``'s mtime from the batched ``stat -c '%Y %n'`` block."""
    mtime_by_agent_activity: dict[tuple[str, str], int] = {}
    for line in block.splitlines():
        mtime_text, _, path_text = line.strip().partition(" ")
        path_parts = path_text.split("/")
        mtime = parse_optional_int(mtime_text)
        if mtime is not None and len(path_parts) >= 3 and path_parts[-2] == "activity":
            mtime_by_agent_activity[(path_parts[-3], path_parts[-1])] = mtime
    return mtime_by_agent_activity


@pure
def _parse_first_tmux_pane_by_window(block: str) -> dict[tuple[str, str], str]:
    """Each ``(session, window)``'s first pane's ``dead|command|pid`` from the ``tmux list-panes -a`` block.

    The first pane listed is the one targeting that window alone would have reported first.
    """
    pane_info_by_window: dict[tuple[str, str], str] = {}
    for line in block.splitlines():
        fields = line.split(_TMUX_PANE_FIELD_SEPARATOR)
        if len(fields) == 3:
            session_name, window_name, pane_info = fields
            pane_info_by_window.setdefault((session_name, window_name), pane_info.strip())
    return pane_info_by_window


@pure
def _join_batched_agent_fields(
    agent_raw: Mapping[str, Any],
    agent_dir_name: str,
    mtime_by_agent_activity: Mapping[tuple[str, str], int],
    pane_info_by_window: Mapping[tuple[str, str], str],
    tmux_session_prefix: str | None,
    tmux_window_name: str | None,
) -> dict[str, Any]:
    """The agent dict with the fields the script collected for all agents at once filled in."""
    activity_mtimes = {
        key: mtime_by_agent_activity.get((agent_dir_name, file_name))
        for file_name, key in _AGENT_ACTIVITY_MTIME_KEY_BY_FILE_NAME.items()
    }
    agent_data = agent_raw["data"]
    agent_name = agent_data.get("name") if isinstance(agent_data, dict) else None
    is_tmux_listed = tmux_session_prefix is not None and tmux_window_name is not None
    tmux_info = (
        pane_info_by_window.get((f"{tmux_session_prefix}{agent_name}", str(tmux_window_name))) or None
        if is_tmux_listed and agent_name
        else None
    )
    return {**agent_raw, **activity_mtimes, "tmux_info": tmux_info}


def parse_listing_collection_output(stdout: str) -> dict[str, Any]:
    """Parse the structured output of the listing collection script."""
    result: dict[str, Any] = {}
    agent_raw_by_dir_name: dict[str, dict[str, Any]] = {}
    mtime_by_agent_activity: dict[tuple[str, str], int] = {}
    pane_info_by_window: dict[tuple[str, str], str] = {}
    tmux_session_prefix: str | None = None
    tmux_window_name: str | None = None
    lines = stdout.split("\n")
    idx = 0

    while idx < len(lines):
        line = lines[idx]

        if line.startswith("HOST_DIR=") and "host_dir" not in result:
            # The host_dir the script settled on, as a path inside the host.
            # Absent when the script never got far enough to resolve one
            # (container missing, extraction failed), so consumers must treat
            # it as optional and fall back to their configured value.
            host_dir_value = line[len("HOST_DIR=") :].strip()
            result["host_dir"] = host_dir_value if host_dir_value else None
        elif line.startswith("TMUX_SESSION_PREFIX=") and tmux_session_prefix is None:
            tmux_session_prefix = line[len("TMUX_SESSION_PREFIX=") :]
        elif line.startswith("TMUX_WINDOW_NAME=") and tmux_window_name is None:
            tmux_window_name = line[len("TMUX_WINDOW_NAME=") :]
        elif line.startswith("UPTIME=") and "uptime_seconds" not in result:
            result["uptime_seconds"] = parse_optional_float(line[len("UPTIME=") :])
        elif line.startswith("BTIME=") and "btime" not in result:
            result["btime"] = parse_optional_int(line[len("BTIME=") :])
        elif line.startswith("LOCK_HELD=") and "is_lock_held" not in result:
            result["is_lock_held"] = line[len("LOCK_HELD=") :].strip() == "true"
        elif line.startswith("LOCK_MTIME=") and "lock_mtime" not in result:
            result["lock_mtime"] = parse_optional_int(line[len("LOCK_MTIME=") :])
        elif line.startswith("SSH_ACTIVITY_MTIME=") and "ssh_activity_mtime" not in result:
            result["ssh_activity_mtime"] = parse_optional_int(line[len("SSH_ACTIVITY_MTIME=") :])
        elif line.startswith("CONTAINER_STATE=") and "container_state" not in result:
            result["container_state"] = line[len("CONTAINER_STATE=") :].strip()
        elif line.startswith("CONTAINER_EXIT_CODE=") and "container_exit_code" not in result:
            result["container_exit_code"] = parse_optional_int(line[len("CONTAINER_EXIT_CODE=") :])
        elif line.startswith("CONTAINER_MISSING=") and "container_missing" not in result:
            result["container_missing"] = line[len("CONTAINER_MISSING=") :].strip() == "true"
        elif line.startswith("EXTRACTION_FAILED=") and "extraction_failed" not in result:
            result["extraction_failed"] = line[len("EXTRACTION_FAILED=") :].strip() == "true"
        elif line.strip() == SEP_DATA_JSON_START:
            idx += 1
            json_str, idx = _extract_delimited_block(lines, idx, SEP_DATA_JSON_END)
            if json_str:
                try:
                    result["certified_data"] = json.loads(json_str)
                except json.JSONDecodeError as e:
                    logger.warning("Failed to parse host data.json in listing output: {}", e)
        elif line.strip() == SEP_PS_START:
            idx += 1
            ps_content, idx = _extract_delimited_block(lines, idx, SEP_PS_END)
            result["ps_output"] = ps_content
        elif line.strip() == SEP_TMUX_PANES_START:
            idx += 1
            tmux_block, idx = _extract_delimited_block(lines, idx, SEP_TMUX_PANES_END)
            pane_info_by_window = _parse_first_tmux_pane_by_window(tmux_block)
        elif line.strip() == SEP_AGENT_MTIMES_START:
            idx += 1
            mtimes_block, idx = _extract_delimited_block(lines, idx, SEP_AGENT_MTIMES_END)
            mtime_by_agent_activity = _parse_agent_activity_mtimes(mtimes_block)
        elif line.strip().startswith(SEP_AGENT_START):
            agent_dir_name = line.strip().removeprefix(SEP_AGENT_START).removesuffix("---")
            idx += 1
            agent_raw, idx = _parse_agent_section(lines, idx)
            if "data" in agent_raw:
                agent_raw_by_dir_name[agent_dir_name] = agent_raw
        else:
            pass
        idx += 1

    result["agents"] = [
        _join_batched_agent_fields(
            agent_raw,
            agent_dir_name,
            mtime_by_agent_activity,
            pane_info_by_window,
            tmux_session_prefix,
            tmux_window_name,
        )
        for agent_dir_name, agent_raw in agent_raw_by_dir_name.items()
    ]
    return result


def extract_agent_data_from_parsed_listing(parsed_listing: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Pull each agent's ``data.json`` dict out of a parsed listing.

    An entry whose ``data`` is present but not a JSON object (a list/scalar from a
    corrupt or hand-edited ``data.json``) is skipped with a warning rather than
    silently, matching the other listing skip-sites (host_store "Skipped invalid
    agent record file"; the Modal provider's "Skipped agent ..."). A genuine JSON
    parse failure was already warned and dropped upstream in ``_parse_agent_section``.
    """
    agent_data: list[dict[str, Any]] = []
    for agent in parsed_listing.get("agents", []):
        data = agent.get("data")
        if isinstance(data, dict):
            agent_data.append(data)
        else:
            logger.warning(
                "Skipping agent entry with missing or non-object 'data' in listing output (found {})",
                type(data).__name__,
            )
    return agent_data
