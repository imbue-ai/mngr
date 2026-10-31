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
  shell that has ``docker`` available. It finds the container (the one a
  previous listing found, while it is still running, else by label),
  dispatches to ``docker exec`` for running containers, or to
  ``docker cp`` + a stopped-variant script for non-running ones. A running
  container whose records still fingerprint as they did at a previous
  listing answers ``LISTING_UNCHANGED=true`` in place of the listing. This
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
from imbue.mngr.hosts.common import TMUX_PANE_LISTING_FORMAT
from imbue.mngr.hosts.common import find_agent_session_name
from imbue.mngr.hosts.common import find_first_pane_info
from imbue.mngr.hosts.common import parse_tmux_pane_listings
from imbue.mngr.interfaces.data_types import TmuxPaneListing

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
def _build_stat_flavor_dispatch_script(gnu_command: str, bsd_command: str) -> str:
    """Build the dispatch that runs the GNU or the BSD form of a ``stat`` command, whichever the host has.

    GNU stat exits non-zero when any one path is missing, so the flavor is probed once
    rather than falling back on failure (BSD's ``-f`` means something else to GNU stat).
    """
    return f"""if stat -c %Y / >/dev/null 2>&1; then
    {gnu_command}
else
    {bsd_command}
fi"""


@pure
def _build_listing_change_fingerprint_script() -> str:
    """Build the prelude that fingerprints the records a discovery listing is shaped from, and may end there.

    Discovery shapes a running host from its ``data.json`` and the ``data.json`` of
    each agent, so the inode, mtime and size of exactly those files identify the
    answer: a rewrite replaces the inode, an in-place edit moves the mtime. The
    fingerprint is taken before any record is read, so a record changing during the
    listing is caught by the next one rather than missed. A caller that remembers
    the fingerprint of its last listing passes it as ``MNGR_UNCHANGED_FINGERPRINT``,
    and a matching one ends the script with ``LISTING_UNCHANGED=true`` before the
    dozens of reads a full listing costs (each spawn is tens of milliseconds on a
    sandboxed runtime). An empty fingerprint (no record at all) never matches.
    """
    fingerprint_stat = _build_stat_flavor_dispatch_script(
        gnu_command="""LISTING_FINGERPRINT=$(stat --printf '%i %.Y %s %n;' "$HOST_DIR/data.json" "$HOST_DIR"/agents/*/data.json 2>/dev/null)""",
        bsd_command="""LISTING_FINGERPRINT=$(stat -f '%i %Fm %z %N;' "$HOST_DIR/data.json" "$HOST_DIR"/agents/*/data.json 2>/dev/null | tr -d '\\n')""",
    )
    return f"""
{fingerprint_stat}
if [ -n "$MNGR_UNCHANGED_FINGERPRINT" ] && [ "$LISTING_FINGERPRINT" = "$MNGR_UNCHANGED_FINGERPRINT" ]; then
    echo "LISTING_UNCHANGED=true"
    exit 0
fi
echo "LISTING_FINGERPRINT=$LISTING_FINGERPRINT"
"""


@pure
def build_listing_collection_script(
    host_dir: str,
    prefix: str,
    window_name: str = "agent",
    fallback_host_dirs: Sequence[str] = (),
    is_change_fingerprint_included: bool = False,
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

    With ``is_change_fingerprint_included`` the script first fingerprints the
    records a discovery listing is shaped from (see
    :func:`_build_listing_change_fingerprint_script`), reports it as
    ``LISTING_FINGERPRINT=``, and ends early with ``LISTING_UNCHANGED=true`` when
    it equals the ``MNGR_UNCHANGED_FINGERPRINT`` environment variable.
    """
    fingerprint_script = _build_listing_change_fingerprint_script() if is_change_fingerprint_included else ""
    return f"""
{_build_host_dir_resolution_script(host_dir, fallback_host_dirs)}
{fingerprint_script}
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

# Every pane on the server, joined to its agent by session (agent id, else name) and window name on the parsing side
echo '{SEP_TMUX_PANES_START}'
if [ -d "$HOST_DIR/agents" ]; then
    tmux list-panes -a -F {shlex.quote(TMUX_PANE_LISTING_FORMAT)} 2>/dev/null
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
    activity_mtimes_stat = _build_stat_flavor_dispatch_script(
        gnu_command="""stat -c '%Y %n' "$HOST_DIR"/agents/*/activity/user "$HOST_DIR"/agents/*/activity/agent "$HOST_DIR"/agents/*/activity/start 2>/dev/null""",
        bsd_command="""stat -f '%m %N' "$HOST_DIR"/agents/*/activity/user "$HOST_DIR"/agents/*/activity/agent "$HOST_DIR"/agents/*/activity/start 2>/dev/null""",
    )
    return f"""
echo '{SEP_AGENT_MTIMES_START}'
{activity_mtimes_stat}
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
    known_container_id: str | None = None,
    unchanged_fingerprint: str | None = None,
) -> str:
    """Build a script that runs on the outer (VPS root) and collects listing data.

    Finds the container -- ``known_container_id`` while it is still running, else
    by ``<host_id_label>=<host_id>`` label -- then:
    - if the container is missing: emits ``CONTAINER_MISSING=true``.
    - if the container is running: ``docker exec``s the inner listing script
      and exits with its status.
    - otherwise: ``docker cp -L``s the host_dir tree to a temp path on the outer
      host and runs the stopped-variant listing script against it. ``-L``
      because a VPS container's host_dir is a symlink into its volume (``/mngr
      -> /mngr-vol/host_dir``): without it a stopped container yields the link
      itself, never a directory, and the host lists no agents.

    Always prepends ``CONTAINER_ID=``, ``CONTAINER_STATE=`` and
    ``CONTAINER_EXIT_CODE=`` lines so the caller can map the docker container
    status to a ``HostState`` without a second round-trip, and hand the
    container back as ``known_container_id`` next time.

    ``fallback_host_dirs`` are other host_dir locations to try when ``host_dir``
    holds no ``data.json``; both branches emit the ``HOST_DIR=`` they settled on,
    always as a path *inside the container* (the stopped branch reports the
    source of the copy, not the outer temp path it was extracted to).

    ``known_container_id`` is the container a previous listing found (reported as
    ``CONTAINER_ID=``): it is inspected directly, and the label lookup runs only
    when it is gone or no longer running, so a listing of a steadily running host
    costs one ``docker`` call fewer. ``unchanged_fingerprint`` is the
    ``LISTING_FINGERPRINT=`` a previous listing of that same container reported;
    the running branch ends with ``LISTING_UNCHANGED=true`` in place of the full
    listing when the container's records still fingerprint the same. It is only
    honored while the known container is the one inspected, so a container that
    was replaced is always listed in full.
    """
    inner_running = build_listing_collection_script(
        host_dir, prefix, window_name, fallback_host_dirs, is_change_fingerprint_included=True
    )
    inner_stopped = _build_stopped_listing_collection_script(prefix)
    candidate_host_dirs = " ".join(shlex.quote(candidate) for candidate in (host_dir, *fallback_host_dirs))
    quoted_host_id = shlex.quote(str(host_id))
    quoted_host_dir = shlex.quote(host_dir)
    quoted_label = shlex.quote(host_id_label)
    quoted_known_container_id = shlex.quote(known_container_id or "")
    quoted_unchanged_fingerprint = shlex.quote(unchanged_fingerprint or "")
    return f"""CID={quoted_known_container_id}
UNCHANGED_FINGERPRINT={quoted_unchanged_fingerprint}
INSPECTED=
if [ -n "$CID" ]; then
    INSPECTED=$(docker inspect --format '{{{{.State.Status}}}} {{{{.State.ExitCode}}}}' "$CID" 2>/dev/null)
    case "$INSPECTED" in
        "running "*) ;;
        *) CID= ;;
    esac
fi
if [ -z "$CID" ]; then
    UNCHANGED_FINGERPRINT=
    CID=$(docker ps -aq --filter label={quoted_label}={quoted_host_id} | head -1)
    if [ -z "$CID" ]; then
        echo "CONTAINER_MISSING=true"
        exit 0
    fi
    INSPECTED=$(docker inspect --format '{{{{.State.Status}}}} {{{{.State.ExitCode}}}}' "$CID" 2>/dev/null)
fi
STATE=${{INSPECTED%% *}}
EXIT_CODE=${{INSPECTED#* }}
echo "CONTAINER_ID=$CID"
echo "CONTAINER_STATE=$STATE"
echo "CONTAINER_EXIT_CODE=$EXIT_CODE"
if [ "$STATE" = "running" ]; then
    # ``-w /`` overrides the container's cwd (which can refer to a path
    # that no longer exists in the container filesystem, causing
    # ``OCI runtime exec failed: chdir to cwd ... no such file or directory``)
    docker exec -i -w / -e MNGR_UNCHANGED_FINGERPRINT="$UNCHANGED_FINGERPRINT" "$CID" bash <<'{_INNER_RUNNING_EOF}'
{inner_running}
{_INNER_RUNNING_EOF}
    # A container killed mid-listing (e.g. one being stopped) leaves truncated
    # output that still parses as a shorter agent list; only the exit code
    # says it is incomplete.
    exit $?
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
    docker cp -L "$CID":"$_mngr_candidate" "$_mngr_dest/" 2>/dev/null || continue
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
def _join_batched_agent_fields(
    agent_raw: Mapping[str, Any],
    agent_dir_name: str,
    mtime_by_agent_activity: Mapping[tuple[str, str], int],
    tmux_panes: Sequence[TmuxPaneListing],
    tmux_session_prefix: str | None,
    tmux_window_name: str | None,
) -> dict[str, Any]:
    """The agent dict with the fields the script collected for all agents at once filled in.

    The agent's session is the one carrying its id (the dir name), else the one named after it: the
    script lists panes before it reads each data.json, so a rename in between leaves the name read
    newer than the listing, which still holds the session under its old name.
    """
    activity_mtimes = {
        key: mtime_by_agent_activity.get((agent_dir_name, file_name))
        for file_name, key in _AGENT_ACTIVITY_MTIME_KEY_BY_FILE_NAME.items()
    }
    agent_data = agent_raw["data"]
    agent_name = agent_data.get("name") if isinstance(agent_data, dict) else None
    is_tmux_listed = tmux_session_prefix is not None and tmux_window_name is not None
    session_name_for_agent_name = f"{tmux_session_prefix}{agent_name}" if agent_name else None
    session_name = (
        find_agent_session_name(tmux_panes, agent_dir_name, session_name_for_agent_name) if is_tmux_listed else None
    )
    tmux_info = (
        (find_first_pane_info(tmux_panes, session_name, str(tmux_window_name)) or None) if session_name else None
    )
    return {**agent_raw, **activity_mtimes, "tmux_info": tmux_info}


def parse_listing_collection_output(stdout: str) -> dict[str, Any]:
    """Parse the structured output of the listing collection script."""
    result: dict[str, Any] = {}
    agent_raw_by_dir_name: dict[str, dict[str, Any]] = {}
    mtime_by_agent_activity: dict[tuple[str, str], int] = {}
    tmux_panes: tuple[TmuxPaneListing, ...] = ()
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
        elif line.startswith("CONTAINER_ID=") and "container_id" not in result:
            container_id_value = line[len("CONTAINER_ID=") :].strip()
            result["container_id"] = container_id_value if container_id_value else None
        elif line.startswith("LISTING_FINGERPRINT=") and "listing_fingerprint" not in result:
            result["listing_fingerprint"] = line[len("LISTING_FINGERPRINT=") :].strip()
        elif line.startswith("LISTING_UNCHANGED=") and "is_listing_unchanged" not in result:
            result["is_listing_unchanged"] = line[len("LISTING_UNCHANGED=") :].strip() == "true"
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
            tmux_panes = parse_tmux_pane_listings(tmux_block)
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
            tmux_panes,
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
