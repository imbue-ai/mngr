"""Shared utilities for single-command listing data collection.

Providers that run agents on remote hosts can use these helpers to collect
all listing data (host status, agent status, activity timestamps, etc.)
in a single SSH command instead of making many individual round-trips.

The shell script collects structured output with unique delimiters, and
the parser extracts it into a dict suitable for building HostDetails and
AgentDetails.
"""

import json
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
def build_listing_collection_script(host_dir: str, prefix: str) -> str:
    """Build a shell script that collects all listing data in one command."""
    return f"""
# Uptime
echo "UPTIME=$(cat /proc/uptime 2>/dev/null | awk '{{print $1}}')"

# Boot time
echo "BTIME=$(grep '^btime ' /proc/stat 2>/dev/null | awk '{{print $2}}')"

# Lock file mtime
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
        tmux_info=$(tmux list-panes -t "${{session_name}}:0" -F '#{{pane_dead}}|#{{pane_current_command}}|#{{pane_pid}}' 2>/dev/null | head -n 1)
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
        elif line.startswith("LOCK_MTIME=") and "lock_mtime" not in result:
            result["lock_mtime"] = parse_optional_int(line[len("LOCK_MTIME=") :])
        elif line.startswith("SSH_ACTIVITY_MTIME=") and "ssh_activity_mtime" not in result:
            result["ssh_activity_mtime"] = parse_optional_int(line[len("SSH_ACTIVITY_MTIME=") :])
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
