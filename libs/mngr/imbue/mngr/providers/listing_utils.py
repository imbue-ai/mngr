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
    return f"""
{_build_host_dir_resolution_script(host_dir, fallback_host_dirs)}
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

# Agents
if [ -d "$HOST_DIR/agents" ]; then
    for agent_dir in "$HOST_DIR/agents"/*/; do
        [ -d "$agent_dir" ] || continue
        data_file="${{agent_dir}}data.json"
        [ -f "$data_file" ] || continue
        agent_id=$(basename "$agent_dir")
        echo '{SEP_AGENT_START}'"$agent_id"'---'
        echo '{SEP_AGENT_DATA_START}'
        cat "$data_file"
        echo ''
        echo '{SEP_AGENT_DATA_END}'
        echo "USER_MTIME=$(stat -c %Y "${{agent_dir}}activity/user" 2>/dev/null)"
        echo "AGENT_MTIME=$(stat -c %Y "${{agent_dir}}activity/agent" 2>/dev/null)"
        echo "START_MTIME=$(stat -c %Y "${{agent_dir}}activity/start" 2>/dev/null)"
        agent_name=$(jq -r '.name // empty' "$data_file" 2>/dev/null)
        session_name='{prefix}'"$agent_name"
        # `=$session:{window_name}` mirrors TmuxWindowTarget; required for list-panes since `-t`
        # resolves as target-window/-pane (a bare `=name` would be parsed as a literal
        # window/pane name). Targeting the window by name keeps this base-index agnostic.
        tmux_info=$(tmux list-panes -t "=${{session_name}}:{window_name}" -F '#{{pane_dead}}|#{{pane_current_command}}|#{{pane_pid}}' 2>/dev/null | head -n 1)
        echo "TMUX_INFO=$tmux_info"
        if [ -f "${{agent_dir}}active" ]; then
            echo "ACTIVE=true"
        else
            echo "ACTIVE=false"
        fi
        url=$(cat "${{agent_dir}}status/url" 2>/dev/null | tr -d '\\n')
        echo "URL=$url"
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
if [ -d "$HOST_DIR/agents" ]; then
    for agent_dir in "$HOST_DIR/agents"/*/; do
        [ -d "$agent_dir" ] || continue
        data_file="${{agent_dir}}data.json"
        [ -f "$data_file" ] || continue
        agent_id=$(basename "$agent_dir")
        echo '{SEP_AGENT_START}'"$agent_id"'---'
        echo '{SEP_AGENT_DATA_START}'
        cat "$data_file"
        echo ''
        echo '{SEP_AGENT_DATA_END}'
        echo "USER_MTIME=$(stat -c %Y "${{agent_dir}}activity/user" 2>/dev/null)"
        echo "AGENT_MTIME=$(stat -c %Y "${{agent_dir}}activity/agent" 2>/dev/null)"
        echo "START_MTIME=$(stat -c %Y "${{agent_dir}}activity/start" 2>/dev/null)"
        echo "TMUX_INFO="
        echo "ACTIVE=false"
        url=$(cat "${{agent_dir}}status/url" 2>/dev/null | tr -d '\\n')
        echo "URL=$url"
        echo '{SEP_AGENT_END}'
    done
fi
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
        elif aline.startswith("USER_MTIME="):
            agent_raw["user_activity_mtime"] = parse_optional_int(aline[len("USER_MTIME=") :])
        elif aline.startswith("AGENT_MTIME="):
            agent_raw["agent_activity_mtime"] = parse_optional_int(aline[len("AGENT_MTIME=") :])
        elif aline.startswith("START_MTIME="):
            agent_raw["start_activity_mtime"] = parse_optional_int(aline[len("START_MTIME=") :])
        elif aline.startswith("TMUX_INFO="):
            val = aline[len("TMUX_INFO=") :].strip()
            agent_raw["tmux_info"] = val if val else None
        elif aline.startswith("ACTIVE="):
            agent_raw["is_active"] = aline[len("ACTIVE=") :].strip() == "true"
        elif aline.startswith("URL="):
            val = aline[len("URL=") :].strip()
            agent_raw["url"] = val if val else None
        else:
            pass
        idx += 1

    return agent_raw, idx


def parse_listing_collection_output(stdout: str) -> dict[str, Any]:
    """Parse the structured output of the listing collection script."""
    result: dict[str, Any] = {}
    agents: list[dict[str, Any]] = []
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
        elif line.strip().startswith(SEP_AGENT_START):
            idx += 1
            agent_raw, idx = _parse_agent_section(lines, idx)
            if "data" in agent_raw:
                agents.append(agent_raw)
        else:
            pass
        idx += 1

    result["agents"] = agents
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
