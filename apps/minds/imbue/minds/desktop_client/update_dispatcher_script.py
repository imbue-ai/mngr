"""The dispatcher half of the workspace's update program: what runs inside a workspace with no app present.

The program is standard-library Python the app writes into each workspace it arms (as
``data/.state/update-apply/dispatcher.py``), so it is the app's current code before the
workspace itself has updated. The workspace's cron runs it every minute, flock-serialized, from
the workspace root. Each tick:

- **dispatches** an armed schedule inside its window (the hours and IANA zone the app wrote,
  read with ``zoneinfo``) when no run has started since the window opened: it checks that no
  run is in flight and no ``updating workspace`` lease is held, creates the update chat through
  the chat app's create route (a bare ``mngr create`` behind a chat app with none), and marks the
  file with the chat. A window that closes without a start records why in the file. A run with
  a verdict retires the file;
- **watches** any run whose record has no verdict: a lead that is gone is closed out (``STUCK``,
  its worker stopped with its branch kept, its leases released), and a lead that sits idle with an
  idle worker is reminded that it owes a verdict.

The command half (``update_workspace_command``) adds the app's own modes to the same program.
The app's constants -- the state paths, the outcome and skip-reason names, the chat's message
text -- are inserted where the program marks them when it is built, so the program and the
app read one source.
"""

import json
from typing import Final

from imbue.minds.desktop_client.update_chat import NO_BACKUP_NOTE
from imbue.minds.desktop_client.update_chat import OLD_STEP_TWO_INSTRUCTION_TEMPLATE
from imbue.minds.desktop_client.update_chat import OVERRIDE_NOTE_TEMPLATE
from imbue.minds.desktop_client.update_chat import PINNED_NOTE_TEMPLATE
from imbue.minds.desktop_client.update_chat import UPDATE_COMMAND
from imbue.minds.desktop_client.update_status import UpdateArmOutcome
from imbue.minds.desktop_client.update_status import UpdateSkipReason
from imbue.minds.desktop_client.update_status import UpdateStartedBy

# Relative to the workspace root, where ``mngr exec`` lands and cron's tick runs.
UPDATE_STATE_DIR: Final[str] = "data/.state/update-apply"
RUN_RECORD_PATH: Final[str] = f"{UPDATE_STATE_DIR}/run.json"
SCHEDULE_FILE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/schedule.json"
LAST_DISPATCH_FILE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/last-dispatch.json"

# Opens the program's one JSON answer on stdout, so it can be found in noisy output.
UPDATE_COMMAND_RESULT_MARKER: Final[str] = "MINDS_UPDATE_COMMAND_JSON:"

# Asks the command's ``read`` whether an account is signed in.
UPDATE_COMMAND_CHECK_SIGN_IN_FLAG: Final[str] = "--check-sign-in"

# What the watchdog sends an idle lead whose run has no verdict.
WATCHDOG_REMINDER_MESSAGE: Final[str] = (
    "A note from the Imbue Studio app: this update has no recorded result yet, and neither you nor "
    "its background agent is working on it. Carry on if you can. If the update cannot finish, "
    'record that with `run-status verdict STUCK --detail "<why, one plain line>"` from the '
    "update-self script and tell the user; until a result is recorded you will keep being reminded."
)

# The verdict detail the watchdog records for a run whose lead is gone.
WATCHDOG_GONE_LEAD_DETAIL: Final[str] = (
    "The update agent stopped before it finished; its chat has what it got through."
)

_APP_CONSTANTS_PLACEHOLDER: Final[str] = "# @APP_CONSTANTS@"


def _app_constants_block() -> str:
    """The program's assignments for every constant it shares with the app."""
    message_parts = {
        "command": UPDATE_COMMAND,
        "pinned": PINNED_NOTE_TEMPLATE,
        "old_step_two": OLD_STEP_TWO_INSTRUCTION_TEMPLATE,
        "override": OVERRIDE_NOTE_TEMPLATE,
        "no_backup": NO_BACKUP_NOTE,
        "reminder": WATCHDOG_REMINDER_MESSAGE,
        "gone_lead_detail": WATCHDOG_GONE_LEAD_DETAIL,
    }
    lines = [
        f"STATE_DIR = {json.dumps(UPDATE_STATE_DIR)}",
        f"RUN_PATH = {json.dumps(RUN_RECORD_PATH)}",
        f"SCHEDULE_PATH = {json.dumps(SCHEDULE_FILE_PATH)}",
        f"LAST_DISPATCH_PATH = {json.dumps(LAST_DISPATCH_FILE_PATH)}",
        f"RESULT_MARKER = {json.dumps(UPDATE_COMMAND_RESULT_MARKER)}",
        f"CHECK_SIGN_IN_FLAG = {json.dumps(UPDATE_COMMAND_CHECK_SIGN_IN_FLAG)}",
        f"STARTED_BY_SCHEDULED = {json.dumps(UpdateStartedBy.SCHEDULED.value.lower())}",
        f"STARTED_BY_IMMEDIATE = {json.dumps(UpdateStartedBy.IMMEDIATE.value.lower())}",
        *(f"OUTCOME_{outcome.value} = {json.dumps(outcome.value)}" for outcome in UpdateArmOutcome),
        *(f"SKIP_{reason.value} = {json.dumps(reason.value)}" for reason in UpdateSkipReason),
        # Strings only, so JSON is also a Python literal.
        f"MESSAGE_PARTS = {json.dumps(message_parts, indent=4)}",
    ]
    return "\n".join(lines)


def build_dispatcher_program_core(program_core: str) -> str:
    """``program_core`` with the app's constants inserted where it marks them."""
    assert program_core.count(_APP_CONSTANTS_PLACEHOLDER) == 1, "the program marks one place for the app's constants"
    return program_core.replace(_APP_CONSTANTS_PLACEHOLDER, _app_constants_block())


# Everything but the app's modes: the helpers both halves share, the window, the dispatch, the
# watchdog, and the cron tick. Kept as one plain string (not an f-string) so the program's own
# braces need no escaping; parameters arrive through argv and the files it reads.
_DISPATCHER_PROGRAM_CORE_SOURCE: Final[str] = r'''#!/usr/bin/env python3
"""The Imbue Studio app's in-workspace update program.

One standard-library program, two roles. Shipped through ``mngr exec`` it is the app's
command (``arm``, ``now``, ``cancel``, ``read``); copied into
``data/.state/update-apply/dispatcher.py`` it is the dispatcher the workspace's cron runs
every minute (``tick``), which starts a scheduled update inside its window and watches over
any run that has no verdict yet. It runs from the workspace root, under the workspace's own
``python3``, with no access to the app's code.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone

try:
    from zoneinfo import ZoneInfo
    from zoneinfo import ZoneInfoNotFoundError
except ImportError:
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

# @APP_CONSTANTS@
DISPATCHER_PATH = STATE_DIR + "/dispatcher.py"
LOG_PATH = STATE_DIR + "/dispatcher.log"
WATCHDOG_PATH = STATE_DIR + "/watchdog.json"
CRON_NAME = "update-self-dispatch"
DURABLE_CRON_PATH = "data/.state/cron.d/" + CRON_NAME
LIVE_CRON_DIR = "/etc/cron.d"
CRON_LOCK_PATH = "/run/update-self-dispatch.lock"
CRON_PATH_LINE = "PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SKILL_PATH = ".agents/skills/update-self/SKILL.md"
UPDATE_SELF_SCRIPT = ".agents/skills/update-self/scripts/update_self.py"
MESSAGE_CHAT_SCRIPT = "system/scripts/message_chat.py"
AGENT_ENV_WRAPPER = "system/libs/automations/with_agent_env.sh"
HOST_ENV_FILE = "/home/user/.mngr/env"
LOCAL_SETTINGS_PATH = ".mngr/settings.local.toml"
ACCOUNT_GATE_SCRIPT = "system/scripts/require_create_account.py"
ACCOUNT_RESOLVER_SCRIPT = "system/scripts/default_account_args.py"
TICKETS_DIR_DEFAULT = "data/.tickets"
BACKGROUND_TASKS_SCRIPT = "system/scripts/background_tasks.py"
MNGR_LIST_FIELD_SEPARATOR = "|"
MNGR_LIST_FIELDS = ("id", "name", "state", "labels.is_primary", "labels.chat_id")
MNGR_LIST_ARGV = (
    "mngr",
    "list",
    "--provider",
    "local",
    "--on-error",
    "continue",
    "--format",
    MNGR_LIST_FIELD_SEPARATOR.join("{%s}" % field for field in MNGR_LIST_FIELDS),
)
SCHEMA = 1

ENDED_AGENT_STATES = ("DONE", "STOPPED")
UPDATE_LEASE_TITLE = "updating workspace"
CRITICAL_APP_LEASE_PREFIX = "editing critical app "
AUTO_OPEN_LABELS = ("assist", "auto_open")
SKIP_CLAUDE_INSTALLATION_CHECK_SETTING = "agent_types.claude.check_installation=false"

# How long a window waits before trying again after an attempt that can cost something to repeat.
RETRY_BACKOFF_SECONDS_BY_SKIP = {SKIP_DISPATCH_FAILED: 15 * 60, SKIP_NEEDS_SIGN_IN: 5 * 60}
# A chat dispatched this window is not replaced sooner than this, however soon its lead ends: a
# lead that dies at launch would otherwise be replaced every minute.
REDISPATCH_AFTER_SECONDS = 15 * 60
# The watchdog reminds an idle lead after this many consecutive idle ticks, and then no more
# often than every REMINDER_INTERVAL_SECONDS.
IDLE_TICKS_BEFORE_REMINDER = 3
REMINDER_INTERVAL_SECONDS = 20 * 60
# A marked chat missing from the listing counts as gone only this long after it was marked.
UNLISTED_MARKED_LEAD_GRACE_SECONDS = 15 * 60
MAX_NAME_SUFFIX = 20
LOG_MAX_BYTES = 256 * 1024
# The chat app's create waits up to 300s for mngr, after up to 30s of not-ready retries.
CHAT_CREATE_TIMEOUT_SECONDS = 360
MNGR_LIST_TIMEOUT_SECONDS = 180
MNGR_CREATE_TIMEOUT_SECONDS = 300
SHORT_COMMAND_TIMEOUT_SECONDS = 60
RESOLVER_TIMEOUT_SECONDS = 90
DETAIL_MAX_CHARS = 1000

def read_json(path):
    """The JSON object at ``path``, or None when it is absent, unreadable, or not an object."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        sys.stderr.write("could not read %s (%s); treating it as absent\n" % (path, exc))
        return None
    return raw if isinstance(raw, dict) else None


def write_json(path, data):
    """Write ``data`` to ``path`` atomically, newline-terminated."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle, scratch = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(json.dumps(data, indent=2) + "\n")
    os.replace(scratch, path)


def write_text_if_changed(path, text, mode=None):
    """Write ``text`` to ``path`` unless it already holds exactly that; return whether it wrote."""
    try:
        with open(path, encoding="utf-8") as handle:
            if handle.read() == text:
                return False
    except (OSError, UnicodeDecodeError):
        pass
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle, scratch = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(text)
    if mode is not None:
        os.chmod(scratch, mode)
    os.replace(scratch, path)
    return True


def bound_log():
    """Cut the dispatcher's log back to its newer half once it outgrows LOG_MAX_BYTES.

    Called on every tick as well as every line: cron appends each tick's stderr to the
    same file, and a tick that only writes stderr never logs a line.
    """
    try:
        if os.path.getsize(LOG_PATH) <= LOG_MAX_BYTES:
            return
        with open(LOG_PATH, "rb") as handle:
            handle.seek(-LOG_MAX_BYTES // 2, os.SEEK_END)
            tail = handle.read()
        with open(LOG_PATH, "wb") as handle:
            handle.write(tail[tail.find(b"\n") + 1 :])
    except FileNotFoundError:
        return
    except OSError as exc:
        sys.stderr.write("could not trim the dispatcher log: %s\n" % exc)


def log(message):
    """Append one timestamped line to the dispatcher's log, keeping the log bounded."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except OSError as exc:
        sys.stderr.write("could not create %s: %s\n" % (STATE_DIR, exc))
    bound_log()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (stamp, message))
    except OSError as exc:
        sys.stderr.write("could not write the dispatcher log: %s\n" % exc)


def emit(payload):
    """Print the one marker-wrapped JSON result the app reads."""
    payload = dict(payload)
    payload["schema"] = SCHEMA
    sys.stdout.write(RESULT_MARKER + json.dumps(payload) + "\n")
    sys.stdout.flush()


def bounded(text):
    return (text or "").strip()[-DETAIL_MAX_CHARS:]


def run_command(argv, timeout, env=None):
    """Run ``argv``; never raises. A failure to start is exit 127 with the reason on stderr."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            argv, returncode=127, stdout="", stderr="could not run %s: %s" % (argv[0], exc)
        )


_AGENT_ENV_CACHE = []


def agent_env():
    """The environment ``mngr`` and ``tk`` need: this process's own under ``mngr exec``, rebuilt under cron.

    cron hands a job a bare environment, so the agent's is restored from the files mngr keeps,
    through the template's own wrapper where it has one.
    """
    if _AGENT_ENV_CACHE:
        return dict(_AGENT_ENV_CACHE[0])
    env = dict(os.environ)
    if "MNGR_HOST_DIR" not in env:
        if os.path.isfile(AGENT_ENV_WRAPPER):
            argv = ["bash", AGENT_ENV_WRAPPER, "env", "-0"]
        else:
            argv = ["bash", "-c", 'set -a; [ -f "$1" ] && . "$1"; set +a; env -0', "_", HOST_ENV_FILE]
        listed = run_command(argv, SHORT_COMMAND_TIMEOUT_SECONDS)
        if listed.returncode == 0:
            for entry in listed.stdout.split("\0"):
                key, separator, value = entry.partition("=")
                if separator and key:
                    env[key] = value
        else:
            sys.stderr.write("could not restore the agent environment: %s\n" % bounded(listed.stderr))
    env["MNGR_ALLOW_UNKNOWN_CONFIG"] = "1"
    env.setdefault("TICKETS_DIR", os.path.abspath(TICKETS_DIR_DEFAULT))
    _AGENT_ENV_CACHE.append(env)
    return dict(env)


def list_agents():
    """Every agent the workspace's mngr lists, as dicts; None when it could not list them.

    A ``--format`` template scoped to the local provider, as the template's own in-container
    readers list agents: it answers from local state where ``--format json`` can fail.
    """
    listed = run_command(list(MNGR_LIST_ARGV), MNGR_LIST_TIMEOUT_SECONDS, agent_env())
    if listed.returncode != 0:
        # ``--on-error continue`` still lists every agent it could; the services-agent check below decides.
        sys.stderr.write("mngr list exited %d: %s\n" % (listed.returncode, bounded(listed.stderr)))
    agents = []
    for line in listed.stdout.splitlines():
        fields = line.split(MNGR_LIST_FIELD_SEPARATOR)
        if len(fields) != len(MNGR_LIST_FIELDS) or not fields[1].strip():
            continue
        agents.append({field: value.strip() for field, value in zip(MNGR_LIST_FIELDS, fields, strict=True)})
    # A listing without the workspace's own services agent read the wrong host dir (a cron
    # environment that did not restore), and would make every agent read as gone.
    if not any(agent.get("labels.is_primary") == "true" for agent in agents):
        sys.stderr.write("mngr list did not list this workspace's services agent; ignoring it\n")
        return None
    return agents


def find_agent(agents, name):
    for agent in agents:
        if agent.get("name") == name:
            return agent
    return None


def is_agent_ended(agent):
    return agent is None or agent.get("state") in ENDED_AGENT_STATES


def chat_id_of(agent):
    """The chat an agent takes messages for: its ``chat_id`` label, else its own id."""
    return str(agent.get("labels.chat_id") or agent.get("id") or "")


def busy_agents():
    """``(names, ids, chat ids)`` the template's background-task CLI reports busy; empty sets without it.

    An agent the CLI does not report reads by its own state: RUNNING is a turn in flight.
    """
    names, agent_ids, chat_ids = set(), set(), set()
    if not os.path.isfile(BACKGROUND_TASKS_SCRIPT):
        return names, agent_ids, chat_ids
    listed = run_command(
        [sys.executable or "python3", BACKGROUND_TASKS_SCRIPT, "list", "--format", "tsv"],
        SHORT_COMMAND_TIMEOUT_SECONDS,
        agent_env(),
    )
    for line in listed.stdout.splitlines() if listed.returncode == 0 else []:
        fields = [field.strip() for field in line.split("\t")]
        if len(fields) < 4 or fields[3] != "true":
            continue
        if fields[2]:
            names.add(fields[2])
        elif fields[1]:
            agent_ids.add(fields[1])
        if fields[0]:
            chat_ids.add(fields[0])
    return names, agent_ids, chat_ids


def is_agent_busy(agent, busy):
    """Running, or waiting on a background task that will resume it."""
    if agent is None or is_agent_ended(agent):
        return False
    if agent.get("state") == "RUNNING":
        return True
    names, agent_ids, chat_ids = busy
    return agent.get("name") in names or agent.get("id") in agent_ids or chat_id_of(agent) in chat_ids


def is_skill_present():
    return os.path.isfile(SKILL_PATH)


def is_cron_available(live_cron_dir):
    """Whether the workspace has a cron daemon and the drop-in directory it reads."""
    return (shutil.which("cron") is not None or os.path.exists("/usr/sbin/cron")) and os.path.isdir(live_cron_dir)


def _local_settings_name_a_create_type():
    try:
        import tomllib

        with open(LOCAL_SETTINGS_PATH, "rb") as handle:
            settings = tomllib.load(handle)
    except (ImportError, OSError, ValueError):
        return False
    create = settings.get("commands", {}).get("create", {})
    agent_type = create.get("type") if isinstance(create, dict) else None
    return isinstance(agent_type, str) and bool(agent_type)


def resolve_account():
    """``(is_signed_in, bare-create account args)`` for this workspace's default account.

    The local settings file is the answer on every template that writes one; a template that
    gates creates on it but has none has nothing signed in. A template from before that file
    asks its own resolver; one from before the account store has a single shared config dir,
    which is signed in by construction.
    """
    if os.path.isfile(LOCAL_SETTINGS_PATH):
        return _local_settings_name_a_create_type(), []
    if os.path.isfile(ACCOUNT_GATE_SCRIPT):
        return False, []
    if not os.path.isfile(ACCOUNT_RESOLVER_SCRIPT):
        return True, []
    resolved = run_command(
        ["uv", "run", "python", ACCOUNT_RESOLVER_SCRIPT, "claude"], RESOLVER_TIMEOUT_SECONDS, agent_env()
    )
    if resolved.returncode != 0:
        # A resolver that fell over says nothing about the account; the create decides.
        sys.stderr.write("the account resolver failed: %s\n" % bounded(resolved.stderr))
        return True, []
    args = [line.strip() for line in resolved.stdout.splitlines() if line.strip()]
    return bool(args), args


def read_ticket(path):
    """``(frontmatter dict, title, body)`` of one tk ticket file; None when unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    front = {}
    index = 1
    while index < len(lines) and lines[index].strip() != "---":
        key, separator, value = lines[index].partition(":")
        if separator:
            front[key.strip()] = value.strip()
        index += 1
    rest = lines[index + 1 :]
    title = ""
    for line in rest:
        if line.startswith("# "):
            title = line[2:].strip()
            break
    return front, title, "\n".join(rest)


def open_update_leases():
    """Every update lease not closed yet: ``(id, title, frontmatter, body)`` per ``updating workspace`` or
    ``editing critical app`` ticket."""
    tickets_dir = agent_env().get("TICKETS_DIR") or TICKETS_DIR_DEFAULT
    try:
        names = sorted(os.listdir(tickets_dir))
    except OSError:
        return []
    leases = []
    for name in names:
        if not name.endswith(".md"):
            continue
        ticket = read_ticket(os.path.join(tickets_dir, name))
        if ticket is None:
            continue
        front, title, body = ticket
        if front.get("status") == "closed":
            continue
        if title == UPDATE_LEASE_TITLE or title.startswith(CRITICAL_APP_LEASE_PREFIX):
            leases.append((front.get("id") or name[:-3], title, front, body))
    return leases


def agent_identities(agent):
    """Every handle a lease can name its holder by: the agent's name, its id, and its chat's id."""
    return {str(agent.get("name") or ""), str(agent.get("id") or ""), chat_id_of(agent)} - {""}


def _lease_holders(front, body):
    """Who a lease names as its holder: the creator tk stamped, and the lease's own ``Held by`` words."""
    holders = set(re.findall(r"Held by (\S+) ", body))
    if front.get("agent"):
        holders.add(front["agent"])
    return holders


def is_update_lease_held(agents):
    """Whether an ``updating workspace`` lease is held by an agent that is still there.

    A lease whose holder has ended is abandoned, and the update's lead takes it over by the
    skill's own rule. One that names no holder, or a listing that could not be read, counts as held.
    """
    for _, title, front, body in open_update_leases():
        if title != UPDATE_LEASE_TITLE:
            continue
        holders = _lease_holders(front, body)
        if agents is None or not holders:
            return True
        if any(not is_agent_ended(agent) and agent_identities(agent) & holders for agent in agents):
            return True
    return False


def run_verdict(run):
    return run.get("verdict") if isinstance(run, dict) else None


def run_started_at(run):
    value = run.get("started_at") if isinstance(run, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def is_run_in_flight(run, agents):
    """Whether the record names a run with no verdict whose lead is still there; unknown agents count as there."""
    if not isinstance(run, dict) or run_verdict(run) is not None or not run.get("chat_agent_name"):
        return False
    if agents is None:
        return True
    return not is_agent_ended(find_agent(agents, run["chat_agent_name"]))


def display_version(ref):
    return re.sub(r"^minds-(?=v\d)", "", ref)


def update_chat_names(target_ref):
    """The update chat's agent name and display name for ``target_ref``."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", target_ref).strip("-_") or "release"
    return "update-to-" + slug, "Update to " + display_version(target_ref)


def first_free_name(base_name, taken_names):
    taken = {name.casefold() for name in taken_names}
    if base_name.casefold() not in taken:
        return base_name
    for suffix in range(2, MAX_NAME_SUFFIX + 1):
        candidate = "%s-%d" % (base_name, suffix)
        if candidate.casefold() not in taken:
            return candidate
    return "%s-%d" % (base_name, int(time.time()))


def render_message(target_ref, ceiling_ref, is_override, is_backup_configured):
    """The update chat's first message: one shape whether the run was scheduled or pressed."""
    pinned = MESSAGE_PARTS["pinned"].format(target=target_ref, ceiling=ceiling_ref)
    old_step_two = MESSAGE_PARTS["old_step_two"].format(ceiling=ceiling_ref)
    parts = [MESSAGE_PARTS["command"], pinned + "\n" + old_step_two]
    if is_override:
        parts.append(MESSAGE_PARTS["override"].format(target=target_ref))
    if not is_backup_configured:
        parts.append(MESSAGE_PARTS["no_backup"])
    return "\n\n".join(parts)


def _create_result(outcome, detail="", chat_agent_name="", chat_id=""):
    return {"outcome": outcome, "detail": detail, "chat_agent_name": chat_agent_name, "chat_id": chat_id}


# Exit statuses of the template's chat messenger: delivered, refused, and no verdict at all
# (python's own status for a missing script, argparse's for an argument the script lacks).
MESSAGE_CHAT_DELIVERED = 0
MESSAGE_CHAT_NO_VERDICT = 2
SIGN_IN_REFUSALS = ("no provider accounts exist", "Connect an AI provider")
NAME_TAKEN_REFUSAL = "already exists; pick another name"
STILL_CREATING_REFUSAL = "is still being created"


def _created_chat_line(stdout):
    """The ``{"chat_id", "name", "display_name"}`` line the messenger prints for a created chat; {} for none."""
    for line in reversed(stdout.splitlines()):
        try:
            created = json.loads(line)
        except ValueError:
            continue
        if isinstance(created, dict) and "chat_id" in created:
            return created
    return {}


def _create_through_message_chat(base_name, taken_names, display_name, message, labels):
    """Ask the workspace's chat app for the chat through the template's own messenger.

    A result dict, or None when no messenger here can create a chat (a template from before
    the script or its create mode), for the bare create to take over. A messenger from
    before ``--title`` is asked again without it, and a taken name under the next free one.
    """
    if not os.path.isfile(MESSAGE_CHAT_SCRIPT):
        return None
    taken = list(taken_names)
    name = first_free_name(base_name, taken)
    is_title_passed = True
    handle, message_path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(message)
    try:
        while True:
            argv = [sys.executable or "python3", MESSAGE_CHAT_SCRIPT, "--create", "--name", name]
            argv += ["--title", display_name] if is_title_passed else []
            for key, value in labels.items():
                argv += ["--label", "%s=%s" % (key, value)]
            argv += ["--message-file", message_path]
            created = run_command(argv, CHAT_CREATE_TIMEOUT_SECONDS, agent_env())
            detail = bounded(created.stderr)
            if created.returncode == MESSAGE_CHAT_DELIVERED:
                line = _created_chat_line(created.stdout)
                return _create_result(OUTCOME_CREATED, "", str(line.get("name") or name), str(line.get("chat_id") or ""))
            if created.returncode == MESSAGE_CHAT_NO_VERDICT:
                if is_title_passed and "--title" in detail:
                    is_title_passed = False
                    continue
                return None
            if any(refusal in detail for refusal in SIGN_IN_REFUSALS):
                return _create_result(OUTCOME_NEEDS_SIGN_IN, detail)
            if NAME_TAKEN_REFUSAL in detail and len(taken) < len(taken_names) + MAX_NAME_SUFFIX:
                taken.append(name)
                name = first_free_name(base_name, taken)
                continue
            if STILL_CREATING_REFUSAL in detail:
                # The create runs on past the chat app's own wait; the chat is on its way.
                return _create_result(OUTCOME_CREATED, detail, name, "")
            if "Falling back to `mngr create`" in detail:
                return _create_result(OUTCOME_CHAT_APP_UNREACHABLE, detail)
            return _create_result(OUTCOME_CREATE_FAILED, detail)
    finally:
        os.unlink(message_path)


def _created_agent_id(create_stdout):
    for line in create_stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == "created" and isinstance(event.get("agent_id"), str):
            return event["agent_id"]
    return ""


def _create_through_mngr(name, display_name, message, labels, account_args):
    """The fallback for a template whose messenger cannot create a chat: a bare ``mngr create`` from the chat
    template, which sources the workspace's environment itself when cron runs it."""
    argv = ["mngr", "create", name, "--template", "chat", "--transfer", "none", "--no-connect"]
    argv += ["--label", "user_created=true", "--label", "display_name=" + display_name]
    for key, value in labels.items():
        argv += ["--label", "%s=%s" % (key, value)]
    argv += ["-S", SKIP_CLAUDE_INSTALLATION_CHECK_SETTING]
    argv += list(account_args)
    handle, message_path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(message)
    try:
        created = run_command(
            argv + ["--message-file", message_path, "--format", "jsonl"], MNGR_CREATE_TIMEOUT_SECONDS, agent_env()
        )
    finally:
        os.unlink(message_path)
    if created.returncode != 0:
        return _create_result(
            OUTCOME_CHAT_APP_UNREACHABLE, bounded(created.stderr) or "mngr create exited %d" % created.returncode
        )
    return _create_result(OUTCOME_CREATED, "", name, _created_agent_id(created.stdout))


def create_update_chat(schedule_like, started_by, is_auto_open, agents):
    """Create the update chat for a pinned target, record the dispatch, and say how it went.

    ``schedule_like`` carries ``ceiling``, ``override_ref`` and ``is_backup_configured``.
    """
    ceiling_ref = str(schedule_like.get("ceiling") or "")
    override_ref = str(schedule_like.get("override_ref") or "")
    target_ref = override_ref or ceiling_ref
    is_signed_in, account_args = resolve_account()
    if not is_signed_in:
        return _create_result(OUTCOME_NEEDS_SIGN_IN, "No AI account is signed in to this workspace.")
    base_name, display_name = update_chat_names(target_ref)
    taken_names = [str(agent.get("name") or "") for agent in agents or []]
    message = render_message(
        target_ref, ceiling_ref, bool(override_ref), bool(schedule_like.get("is_backup_configured", True))
    )
    labels = {label: "true" for label in AUTO_OPEN_LABELS} if is_auto_open else {}
    result = _create_through_message_chat(base_name, taken_names, display_name, message, labels)
    if result is None:
        name = first_free_name(base_name, taken_names)
        result = _create_through_mngr(name, display_name, message, labels, account_args)
    if result["outcome"] == OUTCOME_CREATED:
        write_json(
            LAST_DISPATCH_PATH,
            {
                "schema": SCHEMA,
                "chat_agent_name": result["chat_agent_name"],
                "chat_id": result["chat_id"],
                "started_by": started_by,
                "ceiling": ceiling_ref,
                "target_ref": target_ref,
                "dispatched_at": time.time(),
            },
        )
        log("created update chat %s (%s, target %s)" % (result["chat_agent_name"], started_by, target_ref))
    else:
        log("did not create an update chat (%s, %s): %s" % (started_by, result["outcome"], result["detail"]))
    return result


def _zone(name):
    if ZoneInfo is None or not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _local(now, zone_name):
    zone = _zone(zone_name)
    moment = datetime.fromtimestamp(now, tz=timezone.utc)
    return moment.astimezone(zone) if zone is not None else moment.astimezone()


def _window_open_on(day, start_hour, zone_name):
    """The instant (epoch) a window opens on local ``day``; a start hour skipped by daylight saving opens at the
    first instant after the gap."""
    zone = _zone(zone_name)
    naive = datetime(day.year, day.month, day.day, start_hour)
    aware = naive.replace(tzinfo=zone) if zone is not None else naive.astimezone()
    return aware.timestamp()


def current_window(now, start_hour, end_hour, zone_name):
    """``(opened_at, closes_at)`` of the window ``now`` is inside, or None outside every window.

    Hours are local to ``zone_name`` and the window is ``[start, end)``; ``start > end`` wraps midnight.
    """
    local = _local(now, zone_name)
    hour = local.hour
    if start_hour < end_hour:
        is_inside = start_hour <= hour < end_hour
        open_day = local.date()
    else:
        is_inside = hour >= start_hour or hour < end_hour
        open_day = local.date() if hour >= start_hour else (local - timedelta(days=1)).date()
    if not is_inside:
        return None
    opened_at = _window_open_on(open_day, start_hour, zone_name)
    length_hours = (end_hour - start_hour) % 24
    return opened_at, opened_at + length_hours * 3600.0


def decide_schedule_action(schedule, run, now, is_marked_lead_gone):
    """What one tick does about an armed schedule: ``remove``, ``dispatch``, ``record_skip``, ``note_window``,
    ``wait`` (the backoff after a failed attempt or a fresh dispatch has not passed), or ``nothing``.

    ``is_marked_lead_gone`` is a callable, asked only when the answer turns on it.
    """
    armed_at = float(schedule.get("armed_at") or 0.0)
    started_at = run_started_at(run)
    marked_chat = str(schedule.get("marked_chat") or "")
    marked_at = schedule.get("marked_at")
    marked_at = float(marked_at) if isinstance(marked_at, (int, float)) and not isinstance(marked_at, bool) else None
    # A record under the marked name that started before the mark is an earlier chat's that had the name.
    is_marked_run = (
        bool(marked_chat)
        and isinstance(run, dict)
        and run.get("chat_agent_name") == marked_chat
        and (started_at is None or marked_at is None or started_at >= marked_at)
    )
    if run_verdict(run) is not None and ((started_at is not None and started_at >= armed_at) or is_marked_run):
        return "remove"
    window = current_window(
        now, int(schedule["window_start_hour"]), int(schedule["window_end_hour"]), str(schedule.get("zone") or "")
    )
    if window is None:
        seen = schedule.get("window_seen_open_at")
        if not isinstance(seen, (int, float)) or isinstance(seen, bool):
            return "nothing"
        is_started_in_seen_window = (started_at is not None and started_at >= seen) or (
            marked_at is not None and marked_at >= seen
        )
        return "note_window" if is_started_in_seen_window else "record_skip"
    opened_at = window[0]
    if started_at is not None and started_at >= opened_at and started_at >= armed_at:
        return "note_window"
    next_attempt_at = schedule.get("next_attempt_at")
    is_held_back = (
        isinstance(next_attempt_at, (int, float)) and not isinstance(next_attempt_at, bool) and now < next_attempt_at
    )
    if marked_at is not None and marked_at >= opened_at:
        is_start_recorded = is_marked_run and started_at is not None and started_at >= marked_at
        if not is_start_recorded and is_marked_lead_gone():
            return "wait" if is_held_back else "dispatch"
        return "note_window"
    return "wait" if is_held_back else "dispatch"


# The schedule fields the dispatcher writes; the rest are the app's. An arm keeps the marks from the
# file it replaces and starts the others over.
SCHEDULE_MARK_FIELDS = ("marked_chat", "marked_at", "window_seen_open_at")
DISPATCHER_SCHEDULE_FIELDS = SCHEDULE_MARK_FIELDS + (
    "pending_skip_reason",
    "next_attempt_at",
    "last_skip_reason",
    "last_skip_at",
)


def save_schedule_marks(schedule):
    """Write the dispatcher's fields of ``schedule`` onto the schedule file as it stands now.

    A tick can work for minutes. A cancel that removed the file meanwhile stays cancelled, an arm
    or refresh that rewrote the app's fields keeps them, and a re-arm keeps only the marks.
    """
    current = read_json(SCHEDULE_PATH)
    if current is None:
        log("the schedule was cancelled while this tick worked; leaving it cancelled")
        return
    is_rearmed = current.get("armed_at") != schedule.get("armed_at")
    fields = SCHEDULE_MARK_FIELDS if is_rearmed else DISPATCHER_SCHEDULE_FIELDS
    current.update({key: schedule.get(key) for key in fields})
    write_json(SCHEDULE_PATH, current)


def tick_schedule(now):
    """One minute of an armed schedule: dispatch inside the window, record a skipped window, or retire the file."""
    schedule = read_json(SCHEDULE_PATH)
    if (
        schedule is None
        or not isinstance(schedule.get("window_start_hour"), int)
        or not isinstance(schedule.get("window_end_hour"), int)
    ):
        return
    run = read_json(RUN_PATH)
    agents_cache = []

    def agents():
        if not agents_cache:
            agents_cache.append(list_agents())
        return agents_cache[0]

    def is_marked_lead_gone():
        listed = agents()
        if listed is None:
            return False
        lead = find_agent(listed, str(schedule.get("marked_chat") or ""))
        if lead is not None:
            return is_agent_ended(lead)
        # A create the chat app answered as still running is not listed until it lands.
        marked_at = schedule.get("marked_at")
        if not isinstance(marked_at, (int, float)) or isinstance(marked_at, bool):
            return True
        return now - marked_at >= UNLISTED_MARKED_LEAD_GRACE_SECONDS

    action = decide_schedule_action(schedule, run, now, is_marked_lead_gone)
    window = current_window(
        now, schedule["window_start_hour"], schedule["window_end_hour"], str(schedule.get("zone") or "")
    )
    if action == "remove":
        try:
            os.remove(SCHEDULE_PATH)
        except FileNotFoundError:
            return
        log("retired the schedule: the run it covers has a verdict (%s)" % run_verdict(run))
        return
    if action == "record_skip":
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
        save_schedule_marks(schedule)
        log("the update window closed without a start: %s" % reason)
        return
    if action == "note_window":
        seen = window[0] if window is not None else None
        if schedule.get("window_seen_open_at") != seen or schedule.get("pending_skip_reason"):
            schedule.update({"window_seen_open_at": seen, "pending_skip_reason": ""})
            save_schedule_marks(schedule)
        return
    if action == "nothing":
        return
    if action == "wait":
        return
    assert action == "dispatch", action
    schedule["window_seen_open_at"] = window[0] if window is not None else None
    reason = dispatch_scheduled(schedule, run, agents())
    if reason is None:
        schedule.update({"marked_at": now, "pending_skip_reason": "", "next_attempt_at": now + REDISPATCH_AFTER_SECONDS})
    else:
        # A create that failed may have left a chat behind, so it is not retried every minute.
        backoff = RETRY_BACKOFF_SECONDS_BY_SKIP.get(reason, 0)
        schedule.update({"pending_skip_reason": reason, "next_attempt_at": now + backoff if backoff else None})
    save_schedule_marks(schedule)


def dispatch_scheduled(schedule, run, agents):
    """Start the scheduled run, marking ``schedule`` with its chat; the skip reason when it could not start."""
    if is_run_in_flight(run, agents):
        return SKIP_UPDATE_IN_FLIGHT
    if is_update_lease_held(agents):
        return SKIP_LEASE_HELD
    if not is_skill_present():
        return SKIP_DISPATCH_FAILED
    result = create_update_chat(schedule, STARTED_BY_SCHEDULED, is_auto_open=False, agents=agents)
    if result["outcome"] == OUTCOME_CREATED:
        schedule["marked_chat"] = result["chat_agent_name"]
        return None
    if result["outcome"] == OUTCOME_NEEDS_SIGN_IN:
        return SKIP_NEEDS_SIGN_IN
    return SKIP_DISPATCH_FAILED


def decide_watchdog_action(run, agents, busy, watchdog_state, now):
    """What the watchdog does about a run with no verdict: ``close_out``, ``remind``, ``count_idle``, ``reset``,
    or ``nothing``."""
    if not isinstance(run, dict) or run_verdict(run) is not None or not run.get("chat_agent_name") or agents is None:
        return "nothing"
    if run.get("apply_phase"):
        # The apply's own recovery owns a run that is landing.
        return "reset"
    lead = find_agent(agents, run["chat_agent_name"])
    if is_agent_ended(lead):
        return "close_out"
    if run.get("is_holding"):
        return "reset"
    worker_name = run.get("worker_agent_name")
    worker = find_agent(agents, worker_name) if worker_name else None
    if is_agent_busy(lead, busy) or is_agent_busy(worker, busy):
        return "reset"
    idle_ticks = int(watchdog_state.get("idle_ticks") or 0) + 1
    last_reminded_at = float(watchdog_state.get("last_reminded_at") or 0.0)
    if idle_ticks >= IDLE_TICKS_BEFORE_REMINDER and now - last_reminded_at >= REMINDER_INTERVAL_SECONDS:
        return "remind"
    return "count_idle"


def _watchdog_state_for(run):
    """The watchdog's bookkeeping for this run; a fresh one once a different run is on the record."""
    state = read_json(WATCHDOG_PATH) or {}
    key = "%s@%s" % (run.get("chat_agent_name"), run_started_at(run))
    if state.get("run") != key:
        state = {"run": key, "idle_ticks": 0, "last_reminded_at": 0.0}
    return state


def send_to_chat(agent, text):
    """Message an agent's chat through the workspace's chat messenger; whether it was delivered."""
    if os.path.isfile(MESSAGE_CHAT_SCRIPT):
        argv = [sys.executable or "python3", MESSAGE_CHAT_SCRIPT, chat_id_of(agent), "-m", text]
    else:
        argv = ["mngr", "message", str(agent.get("name")), "-m", text]
    sent = run_command(argv, MNGR_CREATE_TIMEOUT_SECONDS, agent_env())
    if sent.returncode not in (0, 7):
        log("could not message %s: %s" % (agent.get("name"), bounded(sent.stderr)))
        return False
    return True


def record_stuck_verdict(run, detail):
    """Record STUCK on the run through the template's own recorder, the record's one writer; whether it landed."""
    recorded = run_command(
        [
            sys.executable or "python3",
            UPDATE_SELF_SCRIPT,
            "run-status",
            "verdict",
            "STUCK",
            "--chat",
            str(run.get("chat_agent_name")),
            "--detail",
            detail,
        ],
        SHORT_COMMAND_TIMEOUT_SECONDS,
        agent_env(),
    )
    if recorded.returncode != 0:
        log("could not record the gone lead's verdict; trying again next tick: %s" % bounded(recorded.stderr))
    return recorded.returncode == 0


def release_leases_of(lead):
    """Close the update leases the gone lead created, matched by the creator tk stamped or the lease's own words."""
    owners = agent_identities(lead)
    tk = shutil.which("tk", path=agent_env().get("PATH")) or "system/vendor/tk/ticket"
    for lease_id, title, front, body in open_update_leases():
        if not _lease_holders(front, body) & owners:
            continue
        closed = run_command(
            [tk, "close", lease_id, "Released: the update agent that held it is gone."],
            SHORT_COMMAND_TIMEOUT_SECONDS,
            agent_env(),
        )
        log("released lease %s (%s): exit %d" % (lease_id, title, closed.returncode))


def close_out(run, agents):
    """End a run whose lead is gone: record STUCK, stop its worker keeping its branch, release its leases."""
    lead_name = str(run.get("chat_agent_name"))
    lead = find_agent(agents, lead_name) or {"name": lead_name}
    if not record_stuck_verdict(run, MESSAGE_PARTS["gone_lead_detail"]):
        return
    worker_name = run.get("worker_agent_name")
    worker = find_agent(agents, worker_name) if worker_name else None
    if worker is not None and not is_agent_ended(worker):
        stopped = run_command(["mngr", "stop", str(worker_name)], MNGR_CREATE_TIMEOUT_SECONDS, agent_env())
        log("stopped worker %s, keeping its branch: exit %d" % (worker_name, stopped.returncode))
    release_leases_of(lead)
    log("closed out run %s: its lead is gone" % lead_name)


def tick_watchdog(now):
    run = read_json(RUN_PATH)
    if not isinstance(run, dict) or run_verdict(run) is not None or not run.get("chat_agent_name"):
        return
    agents = list_agents()
    state = _watchdog_state_for(run)
    action = decide_watchdog_action(run, agents, busy_agents() if agents is not None else (set(), set(), set()), state, now)
    if action == "nothing":
        return
    if action == "close_out":
        close_out(run, agents)
        return
    if action == "reset":
        if state.get("idle_ticks"):
            state["idle_ticks"] = 0
            write_json(WATCHDOG_PATH, state)
        return
    state["idle_ticks"] = int(state.get("idle_ticks") or 0) + 1
    if action == "remind":
        lead = find_agent(agents, str(run["chat_agent_name"]))
        if lead is not None and send_to_chat(lead, MESSAGE_PARTS["reminder"]):
            state["last_reminded_at"] = now
            log("reminded idle lead %s" % run["chat_agent_name"])
    write_json(WATCHDOG_PATH, state)


def mode_tick(context):
    bound_log()
    tick_watchdog(context["now"])
    tick_schedule(context["now"])


'''

UPDATE_DISPATCHER_PROGRAM_CORE: Final[str] = build_dispatcher_program_core(_DISPATCHER_PROGRAM_CORE_SOURCE)
