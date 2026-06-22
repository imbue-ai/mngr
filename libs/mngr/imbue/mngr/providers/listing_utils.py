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
def build_listing_collection_script(host_dir: str, prefix: str, window_name: str = "agent") -> str:
    """Build a shell script that collects all listing data in one command.

    ``window_name`` is the name of the agent's primary tmux window (config
    ``tmux.primary_window_name``); lifecycle detection targets that window by
    name so it works regardless of the user's tmux ``base-index``.
    """
    return f"""
# Uptime
echo "UPTIME=$(cat /proc/uptime 2>/dev/null | awk '{{print $1}}')"

# Boot time
echo "BTIME=$(grep '^btime ' /proc/stat 2>/dev/null | awk '{{print $2}}')"

# Host lock: held-state (a real flock, probed non-blockingly) and mtime (for
# display). The lock file persists after release, so existence != held; guard on
# existence so the probe never creates it.
echo "LOCK_HELD=$([ -e '{host_dir}/host_lock' ] && ! flock -n '{host_dir}/host_lock' -c true 2>/dev/null && echo true || echo false)"
echo "LOCK_MTIME=$(stat -c %Y '{host_dir}/host_lock' 2>/dev/null)"

# SSH activity mtime
echo "SSH_ACTIVITY_MTIME=$(stat -c %Y '{host_dir}/activity/ssh' 2>/dev/null)"

# Host data.json
echo '{SEP_DATA_JSON_START}'
cat '{host_dir}/data.json' 2>/dev/null || echo '{{}}'
echo ''
echo '{SEP_DATA_JSON_END}'

# ps output (shared by all agents for lifecycle detection)
echo '{SEP_PS_START}'
ps -e -o pid=,ppid=,comm= 2>/dev/null
echo '{SEP_PS_END}'

# Agents
if [ -d '{host_dir}/agents' ]; then
    for agent_dir in '{host_dir}/agents'/*/; do
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
    """
    inner_running = build_listing_collection_script(host_dir, prefix, window_name)
    inner_stopped = _build_stopped_listing_collection_script(prefix)
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
mkdir -p "$TMP/extract"
if ! docker cp "$CID":{quoted_host_dir} "$TMP/extract/" 2>/dev/null; then
    echo "EXTRACTION_FAILED=true"
    exit 0
fi
EXTRACTED="$TMP/extract/$(basename {quoted_host_dir})"
if [ ! -d "$EXTRACTED" ]; then
    echo "EXTRACTION_FAILED=true"
    exit 0
fi
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

        if line.startswith("UPTIME=") and "uptime_seconds" not in result:
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
