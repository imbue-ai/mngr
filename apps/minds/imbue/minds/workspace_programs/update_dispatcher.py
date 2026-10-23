"""The update program's dispatcher: what runs inside a workspace with no app present.

The program (``update_workspace``) copies its own archive into each workspace it arms (as
``data/.state/update-apply/dispatcher.pyz``), so the dispatcher is the app's current code before the
workspace itself has updated. The workspace's cron runs it every minute, flock-serialized, from
the workspace root. Each tick:

- **dispatches** an armed schedule inside its window (a UTC start minute and a length the app
  wrote, from the user's local hours) when no run has started since the window opened: it checks that no
  run is in flight and no ``updating workspace`` lease is held, creates the update chat through
  the chat app's create route (a bare ``mngr create`` behind a chat app with none), and marks the
  file with the chat. A window that closes without a start records why in the file. A run with
  a verdict retires the file;
- **watches** any run whose record has no verdict: a lead that is gone is closed out (``STUCK``,
  its worker stopped with its branch kept, its leases released), and a lead that sits idle with an
  idle worker is reminded that it owes a verdict.

This module also holds what the command modes share with the tick: the state files, the agents
listing, the account check, and the update chat's create.
"""

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime
from datetime import timezone
from enum import StrEnum
from enum import auto
from typing import Final
from typing import TypedDict
from typing import assert_never

from imbue.minds.workspace_programs.program_support import as_json_object
from imbue.minds.workspace_programs.program_support import emit_marker_json
from imbue.minds.workspace_programs.program_support import run_command
from imbue.minds.workspace_programs.update_protocol import LAST_DISPATCH_FILE_PATH
from imbue.minds.workspace_programs.update_protocol import NO_BACKUP_NOTE
from imbue.minds.workspace_programs.update_protocol import OLD_STEP_ONE_INSTRUCTION
from imbue.minds.workspace_programs.update_protocol import OLD_STEP_TWO_INSTRUCTION_TEMPLATE
from imbue.minds.workspace_programs.update_protocol import OUTCOME_CHAT_APP_UNREACHABLE
from imbue.minds.workspace_programs.update_protocol import OUTCOME_CREATED
from imbue.minds.workspace_programs.update_protocol import OUTCOME_CREATE_FAILED
from imbue.minds.workspace_programs.update_protocol import OUTCOME_NEEDS_SIGN_IN
from imbue.minds.workspace_programs.update_protocol import OVERRIDE_NOTE_TEMPLATE
from imbue.minds.workspace_programs.update_protocol import PINNED_NOTE_TEMPLATE
from imbue.minds.workspace_programs.update_protocol import RUN_RECORD_PATH
from imbue.minds.workspace_programs.update_protocol import SCHEDULE_FILE_PATH
from imbue.minds.workspace_programs.update_protocol import SKIP_DISPATCH_FAILED
from imbue.minds.workspace_programs.update_protocol import SKIP_LEASE_HELD
from imbue.minds.workspace_programs.update_protocol import SKIP_NEEDS_SIGN_IN
from imbue.minds.workspace_programs.update_protocol import SKIP_UPDATE_IN_FLIGHT
from imbue.minds.workspace_programs.update_protocol import SKIP_WINDOW_CLOSED
from imbue.minds.workspace_programs.update_protocol import STARTED_BY_SCHEDULED
from imbue.minds.workspace_programs.update_protocol import UPDATE_COMMAND
from imbue.minds.workspace_programs.update_protocol import UPDATE_COMMAND_RESULT_MARKER
from imbue.minds.workspace_programs.update_protocol import UPDATE_STATE_DIR
from imbue.minds.workspace_programs.update_protocol import WATCHDOG_GONE_LEAD_DETAIL
from imbue.minds.workspace_programs.update_protocol import WATCHDOG_REMINDER_MESSAGE
from imbue.minds.workspace_programs.update_protocol import WATCHDOG_REVIVE_MESSAGE_TEMPLATE

DISPATCHER_ARCHIVE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/dispatcher.pyz"
DISPATCHER_LOG_PATH: Final[str] = f"{UPDATE_STATE_DIR}/dispatcher.log"
_WATCHDOG_STATE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/watchdog.json"
# The id the last create of an update chat labelled it with, written before the create runs.
_DISPATCH_ATTEMPT_PATH: Final[str] = f"{UPDATE_STATE_DIR}/dispatch-attempt.json"
_DISPATCH_ATTEMPT_LABEL: Final[str] = "update_dispatch_attempt"
_SKILL_PATH: Final[str] = ".agents/skills/update-self/SKILL.md"
_UPDATE_SELF_SCRIPT: Final[str] = ".agents/skills/update-self/scripts/update_self.py"
_MESSAGE_CHAT_SCRIPT: Final[str] = "system/scripts/message_chat.py"
# The template's env wrapper, the first that exists.
# CLEANUP: drop the automations path once update-self's floor, the oldest release the app updates from,
# keeps the wrapper under system/libs/routines.
_AGENT_ENV_WRAPPERS: Final[tuple[str, ...]] = (
    "system/libs/routines/with_agent_env.sh",
    "system/libs/automations/with_agent_env.sh",
)
_HOST_ENV_FILE: Final[str] = "/home/user/.mngr/env"
_LOCAL_SETTINGS_PATH: Final[str] = ".mngr/settings.local.toml"
_ACCOUNT_GATE_SCRIPT: Final[str] = "system/scripts/require_create_account.py"
_ACCOUNT_RESOLVER_SCRIPT: Final[str] = "system/scripts/default_account_args.py"
_TICKETS_DIR_DEFAULT: Final[str] = "data/.tickets"
_BACKGROUND_TASKS_SCRIPT: Final[str] = "system/scripts/background_tasks.py"
_MNGR_LIST_FIELD_SEPARATOR: Final[str] = "|"
_MNGR_LIST_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "name",
    "state",
    "labels.is_primary",
    "labels.chat_id",
    "labels." + _DISPATCH_ATTEMPT_LABEL,
)
_MNGR_LIST_ARGV: Final[tuple[str, ...]] = (
    "mngr",
    "list",
    "--provider",
    "local",
    "--on-error",
    "continue",
    "--format",
    _MNGR_LIST_FIELD_SEPARATOR.join("{%s}" % field for field in _MNGR_LIST_FIELDS),
)
UPDATE_PROGRAM_SCHEMA: Final[int] = 1

_ENDED_AGENT_STATES: Final[tuple[str, ...]] = ("DONE", "STOPPED")
_UPDATE_LEASE_TITLE: Final[str] = "updating workspace"
_CRITICAL_APP_LEASE_PREFIX: Final[str] = "editing critical app "
_AUTO_OPEN_LABELS: Final[tuple[str, ...]] = ("assist", "auto_open")
_SKIP_CLAUDE_INSTALLATION_CHECK_SETTING: Final[str] = "agent_types.claude.check_installation=false"

# A chat dispatched this window is not replaced sooner than this, however soon its lead ends: a
# lead that dies at launch would otherwise be replaced every minute.
_REDISPATCH_AFTER_SECONDS: Final[float] = 15 * 60
# The watchdog reminds an idle lead after this many consecutive idle ticks, and then no more
# often than every _REMINDER_INTERVAL_SECONDS.
_IDLE_TICKS_BEFORE_REMINDER: Final[int] = 3
_REMINDER_INTERVAL_SECONDS: Final[float] = 20 * 60
# A marked chat missing from the listing counts as gone only this long after it was marked.
_UNLISTED_MARKED_LEAD_GRACE_SECONDS: Final[float] = 15 * 60
_MAX_NAME_SUFFIX: Final[int] = 20
DISPATCHER_LOG_MAX_BYTES: Final[int] = 256 * 1024
# The chat app's create waits up to 300s for mngr, after up to 30s of not-ready retries.
_CHAT_CREATE_TIMEOUT_SECONDS: Final[float] = 360
_MNGR_LIST_TIMEOUT_SECONDS: Final[float] = 180
_MNGR_CREATE_TIMEOUT_SECONDS: Final[float] = 300
_SHORT_COMMAND_TIMEOUT_SECONDS: Final[float] = 60
_RESOLVER_TIMEOUT_SECONDS: Final[float] = 90
_DETAIL_MAX_CHARS: Final[int] = 1000
_SECONDS_PER_DAY: Final[int] = 24 * 60 * 60

# Exit statuses of the template's chat messenger: delivered, refused, and no verdict at all
# (python's own status for a missing script, argparse's for an argument the script lacks).
_MESSAGE_CHAT_DELIVERED: Final[int] = 0
_MESSAGE_CHAT_NO_VERDICT: Final[int] = 2
# ``mngr message`` delivered the message, but the agent is left on a dialog it could not resolve.
_MNGR_MESSAGE_DELIVERED_BUT_BLOCKED: Final[int] = 7
_SIGN_IN_REFUSALS: Final[tuple[str, ...]] = ("no provider accounts exist", "Connect an AI provider")
_NAME_TAKEN_REFUSAL: Final[str] = "already exists; pick another name"
_STILL_CREATING_REFUSAL: Final[str] = "is still being created"
_FALLING_BACK_TO_BARE_CREATE: Final[str] = "Falling back to `mngr create`"

# The schedule fields the dispatcher writes; the rest are the app's. An arm keeps the marks from the
# file it replaces and starts the others over.
_SCHEDULE_MARK_FIELDS: Final[tuple[str, ...]] = ("marked_chat", "marked_at", "window_seen_open_at")
_DISPATCHER_SCHEDULE_FIELDS: Final[tuple[str, ...]] = _SCHEDULE_MARK_FIELDS + (
    "pending_skip_reason",
    "next_attempt_at",
    "last_skip_reason",
    "last_skip_at",
)

# Who the background-task CLI reports busy: agent names, agent ids, and chat ids.
BusyAgents = tuple[AbstractSet[str], AbstractSet[str], AbstractSet[str]]


class ScheduleAction(StrEnum):
    """What one tick does about an armed schedule."""

    REMOVE = auto()
    DISPATCH = auto()
    RECORD_SKIP = auto()
    NOTE_WINDOW = auto()
    # The hold after a fresh dispatch has not passed.
    WAIT = auto()
    NOTHING = auto()


class WatchdogAction(StrEnum):
    """What one tick's watchdog does about a run with no verdict."""

    CLOSE_OUT = auto()
    REVIVE = auto()
    REMIND = auto()
    COUNT_IDLE = auto()
    RESET = auto()
    NOTHING = auto()


class AgentEnvironment:
    """The environment ``mngr`` and ``tk`` need, read on first use and kept for the rest of the run."""

    _variables: dict[str, str] | None = None

    def variables(self) -> dict[str, str]:
        if self._variables is None:
            self._variables = _read_agent_environment()
        return dict(self._variables)


class ProgramContext(TypedDict):
    """What one run of the update program acts on."""

    # The arguments after the mode.
    argv: list[str]
    # The program's own archive, which the installing modes copy in as the dispatcher.
    program_path: str
    live_cron_dir: str
    now: float
    agent_environment: AgentEnvironment


class CreateResult(TypedDict):
    """How one attempt at the update chat went, in the fields the program's answer carries."""

    outcome: str
    detail: str
    chat_agent_name: str
    chat_id: str


class UpdateLease(TypedDict):
    """One open update lease: a tk ticket titled ``updating workspace`` or ``editing critical app ...``."""

    lease_id: str
    title: str
    frontmatter: dict[str, str]
    body: str


def _epoch_or_none(value: object) -> float | None:
    """``value`` as seconds since the epoch, or None when it is not a JSON number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _count_or_zero(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def read_json_object(path: str) -> dict[str, object] | None:
    """The JSON object at ``path``, or None when it is absent, unreadable, or not an object."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw: object = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        write_log_line("could not read %s (%s); treating it as absent" % (path, exc))
        return None
    return as_json_object(raw)


def write_json_atomically(path: str, data: Mapping[str, object]) -> None:
    """Write ``data`` to ``path`` atomically, newline-terminated."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle, scratch = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(json.dumps(data, indent=2) + "\n")
    os.replace(scratch, path)


def write_file_if_changed(path: str, content: bytes, mode: int | None) -> bool:
    """Write ``content`` to ``path`` unless it already holds exactly that; return whether it wrote."""
    try:
        with open(path, "rb") as handle:
            if handle.read() == content:
                return False
    except OSError:
        pass
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle, scratch = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    with os.fdopen(handle, "wb") as out:
        out.write(content)
    if mode is not None:
        os.chmod(scratch, mode)
    os.replace(scratch, path)
    return True


def _bound_dispatcher_log() -> None:
    """Cut the dispatcher's log back to its newer half once it outgrows DISPATCHER_LOG_MAX_BYTES.

    Called on every tick as well as every line: cron appends each tick's stderr to the
    same file, and a tick that only writes stderr never logs a line.
    """
    try:
        if os.path.getsize(DISPATCHER_LOG_PATH) <= DISPATCHER_LOG_MAX_BYTES:
            return
        with open(DISPATCHER_LOG_PATH, "rb") as handle:
            handle.seek(-DISPATCHER_LOG_MAX_BYTES // 2, os.SEEK_END)
            tail = handle.read()
        with open(DISPATCHER_LOG_PATH, "wb") as handle:
            handle.write(tail[tail.find(b"\n") + 1 :])
    except FileNotFoundError:
        return
    except OSError as exc:
        sys.stderr.write("could not trim the dispatcher log: %s\n" % exc)


def write_log_line(message: str) -> None:
    """Append one timestamped line to the dispatcher's log, keeping the log bounded."""
    try:
        os.makedirs(UPDATE_STATE_DIR, exist_ok=True)
    except OSError as exc:
        sys.stderr.write("could not create %s: %s\n" % (UPDATE_STATE_DIR, exc))
    _bound_dispatcher_log()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(DISPATCHER_LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (stamp, message))
    except OSError as exc:
        sys.stderr.write("could not write the dispatcher log: %s\n" % exc)


def emit_update_answer(payload: Mapping[str, object]) -> None:
    """Print the one marker-wrapped JSON answer the app reads."""
    emit_marker_json(UPDATE_COMMAND_RESULT_MARKER, {**payload, "schema": UPDATE_PROGRAM_SCHEMA})


def _bounded_detail(text: str | None) -> str:
    return (text or "").strip()[-_DETAIL_MAX_CHARS:]


def _restored_agent_variables() -> dict[str, str]:
    """The agent's environment as mngr keeps it on disk, through the template's own wrapper where it has one."""
    wrapper = next((path for path in _AGENT_ENV_WRAPPERS if os.path.isfile(path)), None)
    if wrapper is not None:
        argv = ["bash", wrapper, "env", "-0"]
    else:
        argv = ["bash", "-c", 'set -a; [ -f "$1" ] && . "$1"; set +a; env -0', "_", _HOST_ENV_FILE]
    listed = run_command(argv, _SHORT_COMMAND_TIMEOUT_SECONDS)
    if listed.returncode != 0:
        write_log_line("could not restore the agent environment: %s" % _bounded_detail(listed.stderr))
        return {}
    variables: dict[str, str] = {}
    for entry in listed.stdout.split("\0"):
        key, separator, value = entry.partition("=")
        if separator and key:
            variables[key] = value
    return variables


def _read_agent_environment() -> dict[str, str]:
    """This process's own environment under ``mngr exec``; the agent's, restored, under cron.

    cron hands a job a bare environment, so the agent's is restored from the files mngr keeps.
    """
    env = dict(os.environ)
    if "MNGR_HOST_DIR" not in env:
        env.update(_restored_agent_variables())
    env["MNGR_ALLOW_UNKNOWN_CONFIG"] = "1"
    env.setdefault("TICKETS_DIR", os.path.abspath(_TICKETS_DIR_DEFAULT))
    return env


def list_agents(agent_environment: AgentEnvironment) -> list[dict[str, str]] | None:
    """Every agent the workspace's mngr lists, as dicts; None when it could not list them.

    A ``--format`` template scoped to the local provider: it answers from local state where
    ``--format json`` can fail.
    """
    listed = run_command(list(_MNGR_LIST_ARGV), _MNGR_LIST_TIMEOUT_SECONDS, env=agent_environment.variables())
    if listed.returncode != 0:
        # ``--on-error continue`` still lists every agent it could; the services-agent check below decides.
        write_log_line("mngr list exited %d: %s" % (listed.returncode, _bounded_detail(listed.stderr)))
    agents: list[dict[str, str]] = []
    for line in listed.stdout.splitlines():
        fields = line.split(_MNGR_LIST_FIELD_SEPARATOR)
        if len(fields) != len(_MNGR_LIST_FIELDS) or not fields[1].strip():
            continue
        agents.append({field: value.strip() for field, value in zip(_MNGR_LIST_FIELDS, fields, strict=True)})
    # A listing without the workspace's own services agent read the wrong host dir (a cron
    # environment that did not restore), and would make every agent read as gone.
    if not any(agent.get("labels.is_primary") == "true" for agent in agents):
        write_log_line("mngr list did not list this workspace's services agent; ignoring it")
        return None
    return agents


def _find_agent(agents: Sequence[Mapping[str, str]], name: str) -> Mapping[str, str] | None:
    for agent in agents:
        if agent.get("name") == name:
            return agent
    return None


def _is_agent_ended(agent: Mapping[str, str] | None) -> bool:
    return agent is None or agent.get("state") in _ENDED_AGENT_STATES


def chat_id_of(agent: Mapping[str, str]) -> str:
    """The chat an agent takes messages for: its ``chat_id`` label, else its own id."""
    return str(agent.get("labels.chat_id") or agent.get("id") or "")


def _find_chat(agents: Sequence[Mapping[str, str]], name: str, chat_id: str) -> Mapping[str, str] | None:
    """A chat's agent: by ``name``, else by ``chat_id`` ('' for none); None when neither is listed.

    Renaming a chat renames its agent, so a name can go stale under a live chat; its chat id
    does not. A chat that moved to a new agent lists its ended predecessor beside it.
    """
    agent = _find_agent(agents, name)
    if agent is not None or not chat_id:
        return agent
    members = [member for member in agents if chat_id in (member.get("id"), chat_id_of(member))]
    live_members = [member for member in members if not _is_agent_ended(member)]
    candidates = live_members or members
    return candidates[0] if candidates else None


def _dispatched_chat_id(name: str) -> str:
    """The chat id the last dispatch gave the chat it made as ``name``; '' when it made none under that name."""
    dispatch = read_json_object(LAST_DISPATCH_FILE_PATH) or {}
    return str(dispatch.get("chat_id") or "") if dispatch.get("chat_agent_name") == name else ""


def find_run_lead(agents: Sequence[Mapping[str, str]], run: Mapping[str, object]) -> Mapping[str, str] | None:
    """The run's lead: by the name its record holds, else by the chat id the last dispatch gave that name."""
    name = str(run.get("chat_agent_name") or "")
    return _find_chat(agents, name, _dispatched_chat_id(name))


def busy_agents(agent_environment: AgentEnvironment) -> BusyAgents:
    """The names, ids and chat ids the template's background-task CLI reports busy; empty sets without it.

    An agent the CLI does not report reads by its own state: RUNNING is a turn in flight.
    """
    names: set[str] = set()
    agent_ids: set[str] = set()
    chat_ids: set[str] = set()
    if not os.path.isfile(_BACKGROUND_TASKS_SCRIPT):
        return names, agent_ids, chat_ids
    listed = run_command(
        [sys.executable or "python3", _BACKGROUND_TASKS_SCRIPT, "list", "--format", "tsv"],
        _SHORT_COMMAND_TIMEOUT_SECONDS,
        env=agent_environment.variables(),
    )
    for line in listed.stdout.splitlines() if listed.returncode == 0 else []:
        fields = [field.strip() for field in line.split("\t")]
        if len(fields) < 4 or fields[3] != "true":
            continue
        if fields[2]:
            names.add(fields[2])
        elif fields[1]:
            agent_ids.add(fields[1])
        else:
            pass
        if fields[0]:
            chat_ids.add(fields[0])
    return names, agent_ids, chat_ids


def _is_agent_busy(agent: Mapping[str, str] | None, busy: BusyAgents) -> bool:
    """Running, or waiting on a background task that will resume it."""
    if agent is None or _is_agent_ended(agent):
        return False
    if agent.get("state") == "RUNNING":
        return True
    names, agent_ids, chat_ids = busy
    return agent.get("name") in names or agent.get("id") in agent_ids or chat_id_of(agent) in chat_ids


def is_update_skill_present() -> bool:
    return os.path.isfile(_SKILL_PATH)


def is_cron_available(live_cron_dir: str) -> bool:
    """Whether the workspace has a cron daemon and the drop-in directory it reads."""
    return (shutil.which("cron") is not None or os.path.exists("/usr/sbin/cron")) and os.path.isdir(live_cron_dir)


def _do_local_settings_name_a_create_type() -> bool:
    try:
        with open(_LOCAL_SETTINGS_PATH, "rb") as handle:
            settings = tomllib.load(handle)
    except (OSError, ValueError):
        return False
    create = settings.get("commands", {}).get("create", {})
    agent_type = create.get("type") if isinstance(create, dict) else None
    return isinstance(agent_type, str) and bool(agent_type)


def resolve_account(agent_environment: AgentEnvironment) -> tuple[bool, list[str]]:
    """``(is_signed_in, bare-create account args)`` for this workspace's default account.

    The local settings file is the answer on every template that writes one; a template that
    gates creates on it but has none has nothing signed in. A template from before that file
    asks its own resolver; one from before the account store has a single shared config dir,
    which is signed in by construction.
    """
    if os.path.isfile(_LOCAL_SETTINGS_PATH):
        return _do_local_settings_name_a_create_type(), []
    if os.path.isfile(_ACCOUNT_GATE_SCRIPT):
        return False, []
    if not os.path.isfile(_ACCOUNT_RESOLVER_SCRIPT):
        return True, []
    # CLEANUP: drop the resolver and shared-config-dir paths once update-self's floor, the oldest
    # release the app updates from, reaches minds-v0.6.0, the first to write the local settings
    # file. On older templates a bare create launches claude, so the resolver is asked for a
    # claude account; a default account on another harness reads as no account.
    resolved = run_command(
        ["uv", "run", "python", _ACCOUNT_RESOLVER_SCRIPT, "claude"],
        _RESOLVER_TIMEOUT_SECONDS,
        env=agent_environment.variables(),
    )
    if resolved.returncode != 0:
        # A resolver that fell over says nothing about the account; the create decides.
        write_log_line("the account resolver failed: %s" % _bounded_detail(resolved.stderr))
        return True, []
    args = [line.strip() for line in resolved.stdout.splitlines() if line.strip()]
    return bool(args), args


def _read_ticket(path: str) -> tuple[dict[str, str], str, str] | None:
    """``(frontmatter, title, body)`` of one tk ticket file; None when unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    frontmatter: dict[str, str] = {}
    index = 1
    while index < len(lines) and lines[index].strip() != "---":
        key, separator, value = lines[index].partition(":")
        if separator:
            frontmatter[key.strip()] = value.strip()
        index += 1
    rest = lines[index + 1 :]
    title = next((line[2:].strip() for line in rest if line.startswith("# ")), "")
    return frontmatter, title, "\n".join(rest)


def _open_update_leases(agent_environment: AgentEnvironment) -> list[UpdateLease]:
    """Every update lease not closed yet."""
    tickets_dir = agent_environment.variables().get("TICKETS_DIR") or _TICKETS_DIR_DEFAULT
    try:
        names = sorted(os.listdir(tickets_dir))
    except OSError:
        return []
    leases: list[UpdateLease] = []
    for name in names:
        if not name.endswith(".md"):
            continue
        ticket = _read_ticket(os.path.join(tickets_dir, name))
        if ticket is None:
            continue
        frontmatter, title, body = ticket
        if frontmatter.get("status") == "closed":
            continue
        if title == _UPDATE_LEASE_TITLE or title.startswith(_CRITICAL_APP_LEASE_PREFIX):
            leases.append(
                {
                    "lease_id": frontmatter.get("id") or name[:-3],
                    "title": title,
                    "frontmatter": frontmatter,
                    "body": body,
                }
            )
    return leases


def _agent_identities(agent: Mapping[str, str]) -> set[str]:
    """Every handle a lease can name its holder by: the agent's name, its id, and its chat's id."""
    return {str(agent.get("name") or ""), str(agent.get("id") or ""), chat_id_of(agent)} - {""}


def _lease_holders(lease: UpdateLease) -> set[str]:
    """Who a lease names as its holder: the creator tk stamped, and the lease's own ``Held by`` words."""
    holders = set(re.findall(r"Held by (\S+) ", lease["body"]))
    creator = lease["frontmatter"].get("agent")
    if creator:
        holders.add(creator)
    return holders


def is_update_lease_held(agents: Sequence[Mapping[str, str]] | None, agent_environment: AgentEnvironment) -> bool:
    """Whether an ``updating workspace`` lease is held by an agent that is still there.

    A lease whose holder has ended is abandoned, and the update's lead takes it over by the
    skill's own rule. One that names no holder, or a listing that could not be read, counts as held.
    """
    for lease in _open_update_leases(agent_environment):
        if lease["title"] != _UPDATE_LEASE_TITLE:
            continue
        holders = _lease_holders(lease)
        if agents is None or not holders:
            return True
        if any(not _is_agent_ended(agent) and _agent_identities(agent) & holders for agent in agents):
            return True
    return False


def _run_verdict(run: Mapping[str, object] | None) -> object:
    return run.get("verdict") if run is not None else None


def run_started_at(run: Mapping[str, object] | None) -> float | None:
    return _epoch_or_none(run.get("started_at")) if run is not None else None


def is_run_in_flight(
    run: Mapping[str, object] | None, agents: Sequence[Mapping[str, str]] | None, is_stopped_lead_revived: bool
) -> bool:
    """Whether the record names a run with no verdict whose lead is still there; unknown agents count as there.

    A stopped lead counts while ``is_stopped_lead_revived``: the watchdog brings it back. A lead whose
    chat was deleted never does.
    """
    if run is None or _run_verdict(run) is not None or not run.get("chat_agent_name"):
        return False
    if agents is None:
        return True
    lead = find_run_lead(agents, run)
    if lead is None:
        return False
    return is_stopped_lead_revived or not _is_agent_ended(lead)


def pending_dispatched_chat(
    run: Mapping[str, object] | None, agents: Sequence[Mapping[str, str]] | None, now: float
) -> tuple[str, str, float] | None:
    """The last dispatch's ``(chat agent name, chat id, dispatched at)`` while its lead may still be on its way
    to recording its run; None otherwise.

    A new lead records its start and takes the update lease only minutes after its chat is made,
    so until then the last dispatch is the one sign of it. Only for a short while after the
    dispatch, so a lead that never records a start cannot hold updates back for good. An unlisted
    chat counts only while the chat app may still be creating it, which it answered without a chat
    id; one the create gave an id to was there, so unlisted it was deleted.
    """
    dispatch = read_json_object(LAST_DISPATCH_FILE_PATH)
    if dispatch is None or not dispatch.get("chat_agent_name"):
        return None
    dispatched_at = _epoch_or_none(dispatch.get("dispatched_at"))
    if dispatched_at is None or now - dispatched_at >= _UNLISTED_MARKED_LEAD_GRACE_SECONDS:
        return None
    name = str(dispatch["chat_agent_name"])
    started_at = run_started_at(run)
    is_run_recorded = (
        run is not None
        and run.get("chat_agent_name") == name
        and started_at is not None
        and started_at >= dispatched_at
    )
    if is_run_recorded:
        return None
    chat_id = str(dispatch.get("chat_id") or "")
    if agents is not None:
        listed = _find_chat(agents, name, chat_id)
        is_deleted = listed is None and bool(chat_id)
        if is_deleted or (listed is not None and _is_agent_ended(listed)):
            return None
    return name, chat_id, dispatched_at


def _display_version(ref: str) -> str:
    return re.sub(r"^minds-(?=v\d)", "", ref)


def _update_chat_title(target_ref: str) -> str:
    """The name the update chat for ``target_ref`` is shown under."""
    return "Update to " + _display_version(target_ref)


def _chat_app_agent_name(title: str) -> str:
    """The agent name the chat app gives a chat it is asked to create as ``title`` (its canonical form)."""
    stripped = re.sub(r"[^a-zA-Z0-9 _-]+", "", title.strip())
    return re.sub(r"\s+", "-", stripped).strip("-_")


def _mngr_agent_name(title: str) -> str:
    """The agent name a bare ``mngr create`` gives the chat shown as ``title``, which mngr names cannot hold."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-_") or "update"


def _first_free_title(title: str, taken_names: Sequence[str], agent_name_of: Callable[[str], str]) -> str:
    """``title``, or the first numbered one after it, whose agent name is not one of ``taken_names``."""
    taken = {name.casefold() for name in taken_names}
    for candidate in [title] + ["%s %d" % (title, suffix) for suffix in range(2, _MAX_NAME_SUFFIX + 1)]:
        if agent_name_of(candidate).casefold() not in taken:
            return candidate
    return "%s %d" % (title, int(time.time()))


def _render_update_message(target_ref: str, ceiling_ref: str, is_override: bool, is_backup_configured: bool) -> str:
    """The update chat's first message: one shape whether the run was scheduled or pressed."""
    pinned = PINNED_NOTE_TEMPLATE.format(target=target_ref, ceiling=ceiling_ref)
    old_step_two = OLD_STEP_TWO_INSTRUCTION_TEMPLATE.format(ceiling=ceiling_ref)
    parts = [UPDATE_COMMAND, "\n".join((pinned, OLD_STEP_ONE_INSTRUCTION, old_step_two))]
    if is_override:
        parts.append(OVERRIDE_NOTE_TEMPLATE.format(target=target_ref))
    if not is_backup_configured:
        parts.append(NO_BACKUP_NOTE)
    return "\n\n".join(parts)


def create_result(outcome: str, detail: str = "", chat_agent_name: str = "", chat_id: str = "") -> CreateResult:
    return {"outcome": outcome, "detail": detail, "chat_agent_name": chat_agent_name, "chat_id": chat_id}


def _write_message_file(message: str) -> str:
    """The path of a new scratch file holding ``message``; the caller removes it."""
    handle, message_path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(message)
    return message_path


def _created_chat_line(stdout: str) -> dict[str, object]:
    """The ``{"chat_id", "name", "display_name"}`` line the messenger prints for a created chat; {} for none."""
    for line in reversed(stdout.splitlines()):
        try:
            created: object = json.loads(line)
        except ValueError:
            continue
        created_object = as_json_object(created)
        if created_object is not None and "chat_id" in created_object:
            return created_object
    return {}


def _ask_messenger_to_create(
    name: str, labels: Mapping[str, str], message_path: str, agent_environment: AgentEnvironment
) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable or "python3", _MESSAGE_CHAT_SCRIPT, "--create", "--name", name]
    for key, value in labels.items():
        argv += ["--label", "%s=%s" % (key, value)]
    argv += ["--message-file", message_path]
    return run_command(argv, _CHAT_CREATE_TIMEOUT_SECONDS, env=agent_environment.variables())


def _is_sign_in_refusal(stderr: str) -> bool:
    return any(refusal in stderr for refusal in _SIGN_IN_REFUSALS)


def _is_name_taken_refusal(created: subprocess.CompletedProcess[str]) -> bool:
    stderr = created.stderr or ""
    return (
        created.returncode not in (_MESSAGE_CHAT_DELIVERED, _MESSAGE_CHAT_NO_VERDICT)
        and not _is_sign_in_refusal(stderr)
        and _NAME_TAKEN_REFUSAL in stderr
    )


def _messenger_create_result(created: subprocess.CompletedProcess[str], name: str) -> CreateResult | None:
    """What the messenger's create of the chat asked for as ``name`` came to; None for the bare create to take over."""
    stderr = created.stderr or ""
    detail = _bounded_detail(stderr)
    if created.returncode == _MESSAGE_CHAT_DELIVERED:
        line = _created_chat_line(created.stdout)
        agent_name = str(line.get("name") or _chat_app_agent_name(name))
        return create_result(OUTCOME_CREATED, "", agent_name, str(line.get("chat_id") or ""))
    if created.returncode == _MESSAGE_CHAT_NO_VERDICT:
        return None
    if _is_sign_in_refusal(stderr):
        return create_result(OUTCOME_NEEDS_SIGN_IN, detail)
    if _STILL_CREATING_REFUSAL in stderr:
        # The create runs on past the chat app's own wait; the chat is on its way.
        return create_result(OUTCOME_CREATED, detail, _chat_app_agent_name(name), "")
    if _FALLING_BACK_TO_BARE_CREATE in stderr:
        write_log_line("the chat app could not create the update chat: %s" % detail)
        return None
    return create_result(OUTCOME_CREATE_FAILED, detail)


def _create_through_message_chat(
    title: str,
    taken_names: Sequence[str],
    message: str,
    labels: Mapping[str, str],
    agent_environment: AgentEnvironment,
) -> CreateResult | None:
    """Ask the workspace's chat app for the chat through the template's own messenger.

    The chat is asked for by ``title``, which the chat app shows and names its agent for. A
    result, or None when no messenger here can create a chat (a template from before the
    script or its create mode) or the messenger's own ``mngr create`` behind an unreachable chat
    app refused the title as an agent name, for the bare create to take over. A taken name is
    asked again under the next free title.
    """
    if not os.path.isfile(_MESSAGE_CHAT_SCRIPT):
        return None
    taken = list(taken_names)
    name = _first_free_title(title, taken, _chat_app_agent_name)
    message_path = _write_message_file(message)
    try:
        created = _ask_messenger_to_create(name, labels, message_path, agent_environment)
        for _ in range(_MAX_NAME_SUFFIX):
            if not _is_name_taken_refusal(created):
                break
            taken.append(_chat_app_agent_name(name))
            name = _first_free_title(title, taken, _chat_app_agent_name)
            created = _ask_messenger_to_create(name, labels, message_path, agent_environment)
        return _messenger_create_result(created, name)
    finally:
        os.unlink(message_path)


def _created_agent_id(create_stdout: str) -> str:
    for line in create_stdout.splitlines():
        try:
            parsed: object = json.loads(line)
        except ValueError:
            continue
        event = as_json_object(parsed)
        if event is None or event.get("event") != "created":
            continue
        agent_id = event.get("agent_id")
        if isinstance(agent_id, str):
            return agent_id
    return ""


def _create_through_mngr(
    title: str,
    message: str,
    labels: Mapping[str, str],
    account_args: Sequence[str],
    agent_environment: AgentEnvironment,
) -> CreateResult:
    """The fallback for a template whose messenger cannot create a chat: a bare ``mngr create`` from the chat
    template, which sources the workspace's environment itself when cron runs it."""
    name = _mngr_agent_name(title)
    argv = ["mngr", "create", name, "--template", "chat", "--transfer", "none", "--no-connect"]
    argv += ["--label", "user_created=true", "--label", "display_name=" + title]
    for key, value in labels.items():
        argv += ["--label", "%s=%s" % (key, value)]
    argv += ["-S", _SKIP_CLAUDE_INSTALLATION_CHECK_SETTING]
    argv += list(account_args)
    message_path = _write_message_file(message)
    try:
        created = run_command(
            argv + ["--message-file", message_path, "--format", "jsonl"],
            _MNGR_CREATE_TIMEOUT_SECONDS,
            env=agent_environment.variables(),
        )
    finally:
        os.unlink(message_path)
    if created.returncode != 0:
        return create_result(
            OUTCOME_CHAT_APP_UNREACHABLE,
            _bounded_detail(created.stderr) or "mngr create exited %d" % created.returncode,
        )
    return create_result(OUTCOME_CREATED, "", name, _created_agent_id(created.stdout))


def _recent_unreported_attempt_id(target_ref: str, now: float) -> str:
    """The id of the last create of an update chat for ``target_ref`` when it is recent and was not reported
    as made (it failed or timed out, perhaps after its chat existed); '' otherwise.

    Every create labels its chat with a fresh attempt id, written down before the create runs, and
    an agent is listed with its labels from the start of its create. Only a recent attempt counts:
    an older one's chat may have run its update already, and is replaced as a reported chat would be.
    """
    attempt = read_json_object(_DISPATCH_ATTEMPT_PATH) or {}
    attempt_id = str(attempt.get("attempt_id") or "")
    attempted_at = _epoch_or_none(attempt.get("attempted_at"))
    if (
        not attempt_id
        or attempt.get("target_ref") != target_ref
        or attempted_at is None
        or now - attempted_at >= _REDISPATCH_AFTER_SECONDS
        or (read_json_object(LAST_DISPATCH_FILE_PATH) or {}).get("attempt_id") == attempt_id
    ):
        return ""
    return attempt_id


def _find_attempt_chat(agents: Sequence[Mapping[str, str]], attempt_id: str) -> Mapping[str, str] | None:
    """The live chat a create labelled with ``attempt_id``; None when none is listed."""
    for agent in agents:
        if agent.get("labels." + _DISPATCH_ATTEMPT_LABEL) == attempt_id and not _is_agent_ended(agent):
            return agent
    return None


def create_update_chat(
    schedule_like: Mapping[str, object],
    started_by: str,
    is_auto_open: bool,
    agents: Sequence[Mapping[str, str]] | None,
    now: float,
    account: tuple[bool, Sequence[str]],
    agent_environment: AgentEnvironment,
) -> CreateResult:
    """Create the update chat for a pinned target, record the dispatch, and say how it went.

    ``schedule_like`` carries ``ceiling``, ``override_ref`` and ``is_backup_configured``;
    ``account`` is what ``resolve_account`` answered.
    """
    ceiling_ref = str(schedule_like.get("ceiling") or "")
    override_ref = str(schedule_like.get("override_ref") or "")
    target_ref = override_ref or ceiling_ref
    is_signed_in, account_args = account
    if not is_signed_in:
        return create_result(OUTCOME_NEEDS_SIGN_IN, "No AI account is signed in to this workspace.")
    title = _update_chat_title(target_ref)
    taken_names = [str(agent.get("name") or "") for agent in agents or []]
    message = _render_update_message(
        target_ref, ceiling_ref, bool(override_ref), bool(schedule_like.get("is_backup_configured", True))
    )
    attempt_id = _recent_unreported_attempt_id(target_ref, now)
    leftover = _find_attempt_chat(agents or [], attempt_id) if attempt_id else None
    if leftover is not None:
        result = create_result(OUTCOME_CREATED, "", str(leftover.get("name") or ""), chat_id_of(leftover))
    elif attempt_id and agents is None:
        # Another create could double a chat the last one left behind, which only a listing would show.
        detail = "Could not list this workspace's chats to look for one the last attempt may have made."
        result = create_result(OUTCOME_CREATE_FAILED, detail)
    else:
        attempt_id = uuid.uuid4().hex
        write_json_atomically(
            _DISPATCH_ATTEMPT_PATH,
            {"schema": UPDATE_PROGRAM_SCHEMA, "attempt_id": attempt_id, "target_ref": target_ref, "attempted_at": now},
        )
        labels = {label: "true" for label in _AUTO_OPEN_LABELS} if is_auto_open else {}
        labels[_DISPATCH_ATTEMPT_LABEL] = attempt_id
        through_messenger = _create_through_message_chat(title, taken_names, message, labels, agent_environment)
        result = through_messenger or _create_through_mngr(
            _first_free_title(title, taken_names, _mngr_agent_name), message, labels, account_args, agent_environment
        )
    if result["outcome"] == OUTCOME_CREATED:
        write_json_atomically(
            LAST_DISPATCH_FILE_PATH,
            {
                "schema": UPDATE_PROGRAM_SCHEMA,
                "chat_agent_name": result["chat_agent_name"],
                "chat_id": result["chat_id"],
                "started_by": started_by,
                "ceiling": ceiling_ref,
                "target_ref": target_ref,
                "dispatched_at": now,
                "attempt_id": attempt_id,
            },
        )
        how = "adopted" if leftover is not None else "created"
        write_log_line("%s update chat %s (%s, target %s)" % (how, result["chat_agent_name"], started_by, target_ref))
    else:
        write_log_line(
            "did not create an update chat (%s, %s): %s" % (started_by, result["outcome"], result["detail"])
        )
    return result


def current_window(now: float, start_minute_utc: int, length_minutes: int) -> tuple[float, float] | None:
    """``(opened_at, closes_at)`` of the window ``now`` is inside, or None outside every window.

    The window opens daily at ``start_minute_utc`` minutes past UTC midnight and lasts
    ``length_minutes``, so it may run past midnight into the next UTC day.
    """
    utc_midnight = now - now % _SECONDS_PER_DAY
    for opened_at in (utc_midnight + start_minute_utc * 60 - _SECONDS_PER_DAY, utc_midnight + start_minute_utc * 60):
        closes_at = opened_at + length_minutes * 60
        if opened_at <= now < closes_at:
            return opened_at, closes_at
    return None


def _schedule_window(schedule: Mapping[str, object]) -> tuple[int, int] | None:
    """The schedule's ``(UTC start minute, length in minutes)``, or None when the file does not hold both."""
    start_minute_utc = schedule.get("window_start_minute_utc")
    length_minutes = schedule.get("window_length_minutes")
    if not isinstance(start_minute_utc, int) or not isinstance(length_minutes, int):
        return None
    return start_minute_utc, length_minutes


def decide_schedule_action(
    schedule: Mapping[str, object],
    run: Mapping[str, object] | None,
    now: float,
    # Asked only when the answer turns on it, as it may list the workspace's agents.
    is_marked_lead_gone: Callable[[], bool],
) -> ScheduleAction:
    """What one tick does about an armed schedule."""
    window_bounds = _schedule_window(schedule)
    if window_bounds is None:
        return ScheduleAction.NOTHING
    armed_at = _epoch_or_none(schedule.get("armed_at")) or 0.0
    started_at = run_started_at(run)
    marked_chat = str(schedule.get("marked_chat") or "")
    marked_at = _epoch_or_none(schedule.get("marked_at"))
    # A record under the marked name that started before the mark is an earlier chat's that had the name.
    is_marked_run = (
        bool(marked_chat)
        and run is not None
        and run.get("chat_agent_name") == marked_chat
        and (started_at is None or marked_at is None or started_at >= marked_at)
    )
    if _run_verdict(run) is not None and ((started_at is not None and started_at >= armed_at) or is_marked_run):
        return ScheduleAction.REMOVE
    window = current_window(now, *window_bounds)
    if window is None:
        seen = _epoch_or_none(schedule.get("window_seen_open_at"))
        if seen is None:
            return ScheduleAction.NOTHING
        is_started_in_seen_window = (started_at is not None and started_at >= seen) or (
            marked_at is not None and marked_at >= seen
        )
        return ScheduleAction.NOTE_WINDOW if is_started_in_seen_window else ScheduleAction.RECORD_SKIP
    opened_at = window[0]
    if started_at is not None and started_at >= opened_at and started_at >= armed_at:
        return ScheduleAction.NOTE_WINDOW
    next_attempt_at = _epoch_or_none(schedule.get("next_attempt_at"))
    is_held_back = next_attempt_at is not None and now < next_attempt_at
    if marked_at is not None and marked_at >= opened_at:
        is_start_recorded = is_marked_run and started_at is not None and started_at >= marked_at
        if not is_start_recorded and is_marked_lead_gone():
            return ScheduleAction.WAIT if is_held_back else ScheduleAction.DISPATCH
        return ScheduleAction.NOTE_WINDOW
    return ScheduleAction.WAIT if is_held_back else ScheduleAction.DISPATCH


def _save_schedule_marks(schedule: Mapping[str, object]) -> None:
    """Write the dispatcher's fields of ``schedule`` onto the schedule file as it stands now.

    A tick can work for minutes. A cancel that removed the file meanwhile stays cancelled, an arm
    or refresh that rewrote the app's fields keeps them, and a re-arm keeps only the marks.
    """
    current = read_json_object(SCHEDULE_FILE_PATH)
    if current is None:
        write_log_line("the schedule was cancelled while this tick worked; leaving it cancelled")
        return
    is_rearmed = current.get("armed_at") != schedule.get("armed_at")
    fields = _SCHEDULE_MARK_FIELDS if is_rearmed else _DISPATCHER_SCHEDULE_FIELDS
    current.update({key: schedule.get(key) for key in fields})
    write_json_atomically(SCHEDULE_FILE_PATH, current)


def _is_marked_lead_gone(
    schedule: Mapping[str, object], agents: Sequence[Mapping[str, str]] | None, now: float
) -> bool:
    if agents is None:
        return False
    lead = _find_agent(agents, str(schedule.get("marked_chat") or ""))
    if lead is not None:
        return _is_agent_ended(lead)
    # A create the chat app answered as still running is not listed until it lands.
    marked_at = _epoch_or_none(schedule.get("marked_at"))
    if marked_at is None:
        return True
    return now - marked_at >= _UNLISTED_MARKED_LEAD_GRACE_SECONDS


def _tick_schedule(now: float, agent_environment: AgentEnvironment) -> None:
    """One minute of an armed schedule: dispatch inside the window, record a skipped window, or retire the file."""
    schedule = read_json_object(SCHEDULE_FILE_PATH)
    window_bounds = _schedule_window(schedule) if schedule is not None else None
    if schedule is None or window_bounds is None:
        return
    run = read_json_object(RUN_RECORD_PATH)
    # Listed at most once, and only when the tick needs the agents.
    agents = functools.cache(lambda: list_agents(agent_environment))
    action = decide_schedule_action(schedule, run, now, lambda: _is_marked_lead_gone(schedule, agents(), now))
    window = current_window(now, *window_bounds)
    match action:
        case ScheduleAction.REMOVE:
            try:
                os.remove(SCHEDULE_FILE_PATH)
            except FileNotFoundError:
                return
            write_log_line("retired the schedule: the run it covers has a verdict (%s)" % _run_verdict(run))
        case ScheduleAction.RECORD_SKIP:
            reason = str(schedule.get("pending_skip_reason") or SKIP_WINDOW_CLOSED)
            schedule.update(
                {
                    "last_skip_reason": reason,
                    "last_skip_at": now,
                    "pending_skip_reason": "",
                    "next_attempt_at": None,
                    "window_seen_open_at": None,
                }
            )
            _save_schedule_marks(schedule)
            write_log_line("the update window closed without a start: %s" % reason)
        case ScheduleAction.NOTE_WINDOW:
            seen = window[0] if window is not None else None
            if schedule.get("window_seen_open_at") != seen or schedule.get("pending_skip_reason"):
                schedule.update({"window_seen_open_at": seen, "pending_skip_reason": ""})
                _save_schedule_marks(schedule)
        case ScheduleAction.NOTHING | ScheduleAction.WAIT:
            pass
        case ScheduleAction.DISPATCH:
            schedule["window_seen_open_at"] = window[0] if window is not None else None
            reason = _dispatch_scheduled(schedule, run, agents(), now, agent_environment)
            if reason is None:
                schedule.update(
                    {"marked_at": now, "pending_skip_reason": "", "next_attempt_at": now + _REDISPATCH_AFTER_SECONDS}
                )
            else:
                # Once per reason, not every minute the window stays open.
                if reason != schedule.get("pending_skip_reason"):
                    write_log_line("held the scheduled update back: %s" % reason)
                # A chat a failed create left behind is adopted by the next attempt, so it can come at once.
                schedule.update({"pending_skip_reason": reason, "next_attempt_at": None})
            _save_schedule_marks(schedule)
        case _ as unreachable:
            assert_never(unreachable)


def _dispatch_scheduled(
    schedule: dict[str, object],
    run: Mapping[str, object] | None,
    agents: Sequence[Mapping[str, str]] | None,
    now: float,
    agent_environment: AgentEnvironment,
) -> str | None:
    """Start the scheduled run, marking ``schedule`` with its chat; the skip reason when it could not start."""
    # This runs under cron, so the watchdog is here to bring a stopped lead back.
    if (
        is_run_in_flight(run, agents, is_stopped_lead_revived=True)
        or pending_dispatched_chat(run, agents, now) is not None
    ):
        return SKIP_UPDATE_IN_FLIGHT
    if is_update_lease_held(agents, agent_environment):
        return SKIP_LEASE_HELD
    if not is_update_skill_present():
        return SKIP_DISPATCH_FAILED
    result = create_update_chat(
        schedule,
        STARTED_BY_SCHEDULED,
        is_auto_open=False,
        agents=agents,
        now=now,
        account=resolve_account(agent_environment),
        agent_environment=agent_environment,
    )
    if result["outcome"] == OUTCOME_CREATED:
        schedule["marked_chat"] = result["chat_agent_name"]
        return None
    if result["outcome"] == OUTCOME_NEEDS_SIGN_IN:
        return SKIP_NEEDS_SIGN_IN
    return SKIP_DISPATCH_FAILED


def decide_watchdog_action(
    run: Mapping[str, object] | None,
    agents: Sequence[Mapping[str, str]] | None,
    busy: BusyAgents,
    watchdog_state: Mapping[str, object],
    now: float,
) -> WatchdogAction:
    """What the watchdog does about a run with no verdict.

    A stopped lead is revived however often it stops: a workspace restart stops every chat, and
    the run should carry on. Only the lead can tell a loop from bad luck, so the revival message
    leaves that judgment to it.
    """
    if run is None or _run_verdict(run) is not None or not run.get("chat_agent_name") or agents is None:
        return WatchdogAction.NOTHING
    if run.get("apply_phase"):
        # The apply's own recovery owns a run that is landing.
        return WatchdogAction.RESET
    lead = find_run_lead(agents, run)
    if lead is None:
        return WatchdogAction.CLOSE_OUT
    if _is_agent_ended(lead):
        return WatchdogAction.REVIVE
    if run.get("is_holding"):
        return WatchdogAction.RESET
    worker_name = str(run.get("worker_agent_name") or "")
    worker = _find_agent(agents, worker_name) if worker_name else None
    if _is_agent_busy(lead, busy) or _is_agent_busy(worker, busy):
        return WatchdogAction.RESET
    idle_ticks = _count_or_zero(watchdog_state.get("idle_ticks")) + 1
    last_reminded_at = _epoch_or_none(watchdog_state.get("last_reminded_at")) or 0.0
    if idle_ticks >= _IDLE_TICKS_BEFORE_REMINDER and now - last_reminded_at >= _REMINDER_INTERVAL_SECONDS:
        return WatchdogAction.REMIND
    return WatchdogAction.COUNT_IDLE


def _watchdog_state_for(run: Mapping[str, object]) -> dict[str, object]:
    """The watchdog's bookkeeping for this run; a fresh one once a different run is on the record."""
    state = read_json_object(_WATCHDOG_STATE_PATH) or {}
    key = "%s@%s" % (run.get("chat_agent_name"), run_started_at(run))
    if state.get("run") != key:
        return {"run": key, "idle_ticks": 0, "last_reminded_at": 0.0}
    return state


def _send_to_chat(agent: Mapping[str, str], text: str, agent_environment: AgentEnvironment) -> bool:
    """Message an agent's chat through the workspace's chat messenger, starting it if it stopped; whether it was
    delivered."""
    if os.path.isfile(_MESSAGE_CHAT_SCRIPT):
        argv = [sys.executable or "python3", _MESSAGE_CHAT_SCRIPT, chat_id_of(agent), "-m", text]
    else:
        argv = ["mngr", "message", str(agent.get("name")), "--start", "-m", text]
    sent = run_command(argv, _MNGR_CREATE_TIMEOUT_SECONDS, env=agent_environment.variables())
    if sent.returncode not in (0, _MNGR_MESSAGE_DELIVERED_BUT_BLOCKED):
        write_log_line("could not message %s: %s" % (agent.get("name"), _bounded_detail(sent.stderr)))
        return False
    return True


def _record_stuck_verdict(run: Mapping[str, object], detail: str, agent_environment: AgentEnvironment) -> bool:
    """Record STUCK on the run through the template's own recorder, the record's one writer; whether it landed."""
    recorded = run_command(
        [
            sys.executable or "python3",
            _UPDATE_SELF_SCRIPT,
            "run-status",
            "verdict",
            "STUCK",
            "--chat",
            str(run.get("chat_agent_name")),
            "--detail",
            detail,
        ],
        _SHORT_COMMAND_TIMEOUT_SECONDS,
        env=agent_environment.variables(),
    )
    if recorded.returncode != 0:
        write_log_line(
            "could not record the gone lead's verdict; trying again next tick: %s" % _bounded_detail(recorded.stderr)
        )
    return recorded.returncode == 0


def _release_leases_of(lead: Mapping[str, str], agent_environment: AgentEnvironment) -> None:
    """Close the update leases the gone lead created, matched by the creator tk stamped or the lease's own words."""
    owners = _agent_identities(lead)
    tk = shutil.which("tk", path=agent_environment.variables().get("PATH")) or "system/vendor/tk/ticket"
    for lease in _open_update_leases(agent_environment):
        if not _lease_holders(lease) & owners:
            continue
        closed = run_command(
            [tk, "close", lease["lease_id"], "Released: the update agent that held it is gone."],
            _SHORT_COMMAND_TIMEOUT_SECONDS,
            env=agent_environment.variables(),
        )
        write_log_line("released lease %s (%s): exit %d" % (lease["lease_id"], lease["title"], closed.returncode))


def _is_run_still_open(run: Mapping[str, object]) -> bool:
    """Whether the run record still names ``run`` with no verdict.

    The agents listing can take minutes, so a tick re-reads the record before it acts on what the
    listing said: a lead may have recorded its verdict, or a new run, meanwhile.
    """
    current = read_json_object(RUN_RECORD_PATH)
    return (
        current is not None
        and _run_verdict(current) is None
        and current.get("chat_agent_name") == run.get("chat_agent_name")
        and run_started_at(current) == run_started_at(run)
    )


def close_out_run(
    run: Mapping[str, object], agents: Sequence[Mapping[str, str]], agent_environment: AgentEnvironment
) -> None:
    """End a run whose lead is gone: record STUCK, stop its worker keeping its branch, release its leases."""
    lead_name = str(run.get("chat_agent_name"))
    if not _is_run_still_open(run):
        write_log_line("the run record changed while this tick listed the agents; leaving %s's close-out" % lead_name)
        return
    lead = find_run_lead(agents, run) or {"name": lead_name}
    if not _record_stuck_verdict(run, WATCHDOG_GONE_LEAD_DETAIL, agent_environment):
        return
    worker_name = str(run.get("worker_agent_name") or "")
    worker = _find_agent(agents, worker_name) if worker_name else None
    if worker is not None and not _is_agent_ended(worker):
        stopped = run_command(
            ["mngr", "stop", worker_name], _MNGR_CREATE_TIMEOUT_SECONDS, env=agent_environment.variables()
        )
        write_log_line("stopped worker %s, keeping its branch: exit %d" % (worker_name, stopped.returncode))
    _release_leases_of(lead, agent_environment)
    write_log_line("closed out run %s: its lead is gone" % lead_name)


def revive_stopped_lead(
    run: Mapping[str, object],
    agents: Sequence[Mapping[str, str]],
    state: dict[str, object],
    agent_environment: AgentEnvironment,
) -> None:
    """Message a stopped lead, which starts it again, counting the restarts in the watchdog's ``state``."""
    lead_name = str(run.get("chat_agent_name"))
    if not _is_run_still_open(run):
        write_log_line("the run record changed while this tick listed the agents; leaving %s stopped" % lead_name)
        return
    lead = find_run_lead(agents, run)
    if lead is None:
        write_log_line("lead %s is no longer listed; leaving its restart to the next tick" % lead_name)
        return
    revivals = _count_or_zero(state.get("revivals")) + 1
    if _send_to_chat(lead, WATCHDOG_REVIVE_MESSAGE_TEMPLATE.format(count=revivals), agent_environment):
        state.update({"revivals": revivals, "idle_ticks": 0})
        write_json_atomically(_WATCHDOG_STATE_PATH, state)
        write_log_line("restarted stopped lead %s (restart %d)" % (lead_name, revivals))


def _tick_watchdog(now: float, agent_environment: AgentEnvironment) -> None:
    run = read_json_object(RUN_RECORD_PATH)
    if run is None or _run_verdict(run) is not None or not run.get("chat_agent_name"):
        return
    agents = list_agents(agent_environment)
    if agents is None:
        return
    state = _watchdog_state_for(run)
    action = decide_watchdog_action(run, agents, busy_agents(agent_environment), state, now)
    match action:
        case WatchdogAction.NOTHING:
            pass
        case WatchdogAction.CLOSE_OUT:
            close_out_run(run, agents, agent_environment)
        case WatchdogAction.REVIVE:
            revive_stopped_lead(run, agents, state, agent_environment)
        case WatchdogAction.RESET:
            if state.get("idle_ticks"):
                state["idle_ticks"] = 0
                write_json_atomically(_WATCHDOG_STATE_PATH, state)
        case WatchdogAction.COUNT_IDLE | WatchdogAction.REMIND:
            state["idle_ticks"] = _count_or_zero(state.get("idle_ticks")) + 1
            lead = find_run_lead(agents, run) if action is WatchdogAction.REMIND else None
            if lead is not None and _send_to_chat(lead, WATCHDOG_REMINDER_MESSAGE, agent_environment):
                state["last_reminded_at"] = now
                write_log_line("reminded idle lead %s" % run["chat_agent_name"])
            write_json_atomically(_WATCHDOG_STATE_PATH, state)
        case _ as unreachable:
            assert_never(unreachable)


def mode_tick(context: ProgramContext) -> None:
    """The dispatcher's minute, run by cron from the installed archive."""
    _bound_dispatcher_log()
    _tick_watchdog(context["now"], context["agent_environment"])
    _tick_schedule(context["now"], context["agent_environment"])
