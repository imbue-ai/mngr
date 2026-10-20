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

import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
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
APPS_FILE = "data/.state/apps.toml"
CHAT_APP_NAME = "chat"
CHAT_APP_FALLBACK_URL = "http://127.0.0.1:8010"
CREATE_CHAT_PATH = "/api/chats/create"
TRACKED_AGENTS_PATH = "/api/agents?tracked=true"
SCHEMA = 1

ENDED_AGENT_STATES = ("DONE", "STOPPED")
UPDATE_LEASE_TITLE = "updating workspace"
CRITICAL_APP_LEASE_PREFIX = "editing critical app "
AUTO_OPEN_LABELS = ("assist", "auto_open")
SKIP_CLAUDE_INSTALLATION_CHECK_SETTING = "agent_types.claude.check_installation=false"

# How long a window waits before trying again after an attempt that can cost something to repeat.
RETRY_BACKOFF_SECONDS_BY_SKIP = {SKIP_DISPATCH_FAILED: 15 * 60, SKIP_NEEDS_SIGN_IN: 5 * 60}
# The watchdog reminds an idle lead after this many consecutive idle ticks, and then no more
# often than every REMINDER_INTERVAL_SECONDS.
IDLE_TICKS_BEFORE_REMINDER = 3
REMINDER_INTERVAL_SECONDS = 20 * 60
MAX_NAME_SUFFIX = 20
LOG_MAX_BYTES = 256 * 1024
CONNECT_TIMEOUT_SECONDS = 3.0
NOT_READY_RETRY_WINDOW_SECONDS = 30.0
NOT_FOUND_RETRY_WINDOW_SECONDS = 5.0
MNGR_LIST_TIMEOUT_SECONDS = 180
MNGR_CREATE_TIMEOUT_SECONDS = 300
SHORT_COMMAND_TIMEOUT_SECONDS = 60
RESOLVER_TIMEOUT_SECONDS = 90
DETAIL_MAX_CHARS = 1000

class ChatAppUnreachable(Exception):
    """The connection to the chat app could not be made at all, so nothing reached it."""




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


def log(message):
    """Append one timestamped line to the dispatcher's log, keeping the log bounded."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            with open(LOG_PATH, "rb") as handle:
                handle.seek(-LOG_MAX_BYTES // 2, os.SEEK_END)
                tail = handle.read()
            with open(LOG_PATH, "wb") as handle:
                handle.write(tail[tail.find(b"\n") + 1 :])
    except OSError:
        pass
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
    """Every agent the workspace's mngr lists, as dicts; None when it could not list them."""
    listed = run_command(
        ["mngr", "list", "--format", "json", "--provider", "local"], MNGR_LIST_TIMEOUT_SECONDS, agent_env()
    )
    if listed.returncode != 0:
        sys.stderr.write("mngr list failed: %s\n" % bounded(listed.stderr or listed.stdout))
        return None
    for line in reversed(listed.stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("agents"), list):
            agents = [agent for agent in candidate["agents"] if isinstance(agent, dict)]
            # A listing without the workspace's own services agent read the wrong host dir (a cron
            # environment that did not restore), and would make every agent read as gone.
            if not any(is_primary_agent(agent) for agent in agents):
                sys.stderr.write("mngr list did not list this workspace's services agent; ignoring it\n")
                return None
            return agents
    sys.stderr.write("mngr list printed no agent listing\n")
    return None


def is_primary_agent(agent):
    labels = agent.get("labels")
    return agent.get("type") == "main" or (isinstance(labels, dict) and labels.get("is_primary") == "true")


def find_agent(agents, name):
    for agent in agents:
        if agent.get("name") == name:
            return agent
    return None


def is_agent_ended(agent):
    return agent is None or agent.get("state") in ENDED_AGENT_STATES


def chat_id_of(agent):
    """The chat an agent takes messages for: its ``chat_id`` label, else its own id."""
    labels = agent.get("labels")
    label = labels.get("chat_id") if isinstance(labels, dict) else None
    return str(label or agent.get("id") or "")




def chat_app_url():
    try:
        import tomllib

        with open(APPS_FILE, "rb") as handle:
            rows = tomllib.load(handle).get("apps", [])
    except (ImportError, OSError, ValueError):
        return CHAT_APP_FALLBACK_URL
    for row in rows:
        if (
            isinstance(row, dict)
            and row.get("name") == CHAT_APP_NAME
            and isinstance(row.get("url"), str)
            and row["url"]
        ):
            return row["url"].rstrip("/")
    return CHAT_APP_FALLBACK_URL


def chat_app_request(method, path, body=None):
    """One request to the chat app on loopback, waiting as long as its answer takes.

    Raises ChatAppUnreachable only when the connection itself cannot be made; returns
    ``(status, parsed body)`` otherwise.
    """
    parsed = urllib.parse.urlsplit(chat_app_url())
    connection = http.client.HTTPConnection(
        parsed.hostname or "127.0.0.1", parsed.port or 80, timeout=CONNECT_TIMEOUT_SECONDS
    )
    try:
        connection.connect()
    except OSError as exc:
        raise ChatAppUnreachable("could not connect to the chat app: %s" % exc) from exc
    try:
        if connection.sock is not None:
            connection.sock.settimeout(None)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    try:
        answer = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        answer = None
    return response.status, answer


def answer_detail(status, answer):
    if isinstance(answer, dict) and isinstance(answer.get("detail"), str):
        return answer["detail"]
    return "the chat app answered HTTP %d" % status


def busy_by_agent_name():
    """Whether each agent is busy, as the chat app's tracked listing says; None where it cannot say.

    A chat app from before the listing's ``is_busy`` answers without it, and then the caller
    falls back to each agent's plain RUNNING state.
    """
    try:
        status, answer = chat_app_request("GET", TRACKED_AGENTS_PATH)
    except (ChatAppUnreachable, OSError, http.client.HTTPException):
        return None
    if status != 200 or not isinstance(answer, dict) or not isinstance(answer.get("agents"), list):
        return None
    busy = {}
    for item in answer["agents"]:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("is_busy"), bool):
            busy[item["name"]] = item["is_busy"]
    return busy or None


def is_agent_busy(agent, busy_by_name):
    """Running, or waiting on a background task; an agent the busy listing does not cover reads by its state."""
    if agent is None or is_agent_ended(agent):
        return False
    if busy_by_name is not None and agent.get("name") in busy_by_name:
        return busy_by_name[agent.get("name")]
    return agent.get("state") == "RUNNING"




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


def is_update_lease_held():
    return any(title == UPDATE_LEASE_TITLE for _, title, _, _ in open_update_leases())




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


def _is_sign_in_refusal(status, answer):
    if status != 400 or not isinstance(answer, dict):
        return False
    detail = str(answer.get("detail") or "")
    return answer.get("kind") == "no_provider_account" or "no provider accounts exist" in detail


def _create_through_route(name, display_name, message, labels):
    """Ask the chat app to create the chat; a result dict, or None when the route cannot take it.

    Retries the answers that mean "not ready yet" (503) and "not here yet" (404) within their
    windows, a refusal of ``title`` (a route from before it) without the title, and a name
    collision under the next free name.
    """
    body = {
        "name": name,
        "title": display_name,
        "message": message,
        "labels": dict(labels),
        "is_installation_check_skipped": True,
        "should_wait": True,
    }
    started = time.monotonic()
    not_found_since = None
    taken_names = []
    while True:
        try:
            status, answer = chat_app_request("POST", CREATE_CHAT_PATH, body)
        except ChatAppUnreachable:
            return None
        except (OSError, http.client.HTTPException) as exc:
            return _create_result(OUTCOME_CREATE_FAILED, "the chat app dropped the create: %s" % exc)
        detail = answer_detail(status, answer)
        if 200 <= status < 300:
            chat_id = str(answer.get("chat_id") or "") if isinstance(answer, dict) else ""
            created_name = str(answer.get("name") or body["name"]) if isinstance(answer, dict) else body["name"]
            return _create_result(OUTCOME_CREATED, "", created_name, chat_id)
        if status == 503 and time.monotonic() - started < NOT_READY_RETRY_WINDOW_SECONDS:
            time.sleep(1.0)
            continue
        if status == 404:
            not_found_since = not_found_since or time.monotonic()
            if time.monotonic() - not_found_since < NOT_FOUND_RETRY_WINDOW_SECONDS:
                time.sleep(0.5)
                continue
            return None
        if status == 400 and "title" in body and "title" in detail:
            del body["title"]
            continue
        if status == 400 and "should_wait" in detail:
            return None
        if _is_sign_in_refusal(status, answer):
            return _create_result(OUTCOME_NEEDS_SIGN_IN, detail)
        if status == 409 and len(taken_names) < MAX_NAME_SUFFIX:
            taken_names.append(body["name"])
            body["name"] = first_free_name(name, taken_names)
            continue
        if status == 504:
            # The create is still running past the route's own wait; the chat is on its way.
            return _create_result(OUTCOME_CREATED, detail, body["name"], "")
        return _create_result(OUTCOME_CREATE_FAILED, detail)


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
    """The fallback for a chat app with no create route: a bare ``mngr create`` from the chat template."""
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
    name = first_free_name(base_name, [str(agent.get("name") or "") for agent in agents or []])
    message = render_message(
        target_ref, ceiling_ref, bool(override_ref), bool(schedule_like.get("is_backup_configured", True))
    )
    labels = {label: "true" for label in AUTO_OPEN_LABELS} if is_auto_open else {}
    result = _create_through_route(name, display_name, message, labels)
    if result is None:
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
    or ``nothing``.

    ``is_marked_lead_gone`` is a callable, asked only when the answer turns on it.
    """
    armed_at = float(schedule.get("armed_at") or 0.0)
    started_at = run_started_at(run)
    marked_chat = str(schedule.get("marked_chat") or "")
    is_marked_run = bool(marked_chat) and isinstance(run, dict) and run.get("chat_agent_name") == marked_chat
    if run_verdict(run) is not None and ((started_at is not None and started_at >= armed_at) or is_marked_run):
        return "remove"
    window = current_window(
        now, int(schedule["window_start_hour"]), int(schedule["window_end_hour"]), str(schedule.get("zone") or "")
    )
    marked_at = schedule.get("marked_at")
    marked_at = float(marked_at) if isinstance(marked_at, (int, float)) and not isinstance(marked_at, bool) else None
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
    if marked_at is not None and marked_at >= opened_at:
        is_start_recorded = is_marked_run and started_at is not None and started_at >= marked_at
        if not is_start_recorded and is_marked_lead_gone():
            return "dispatch"
        return "note_window"
    next_attempt_at = schedule.get("next_attempt_at")
    if isinstance(next_attempt_at, (int, float)) and not isinstance(next_attempt_at, bool) and now < next_attempt_at:
        return "wait"
    return "dispatch"


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
        return listed is not None and is_agent_ended(find_agent(listed, str(schedule.get("marked_chat") or "")))

    action = decide_schedule_action(schedule, run, now, is_marked_lead_gone)
    window = current_window(
        now, schedule["window_start_hour"], schedule["window_end_hour"], str(schedule.get("zone") or "")
    )
    if action == "remove":
        os.remove(SCHEDULE_PATH)
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
        write_json(SCHEDULE_PATH, schedule)
        log("the update window closed without a start: %s" % reason)
        return
    if action == "note_window":
        seen = window[0] if window is not None else None
        if schedule.get("window_seen_open_at") != seen or schedule.get("pending_skip_reason"):
            schedule.update({"window_seen_open_at": seen, "pending_skip_reason": ""})
            write_json(SCHEDULE_PATH, schedule)
        return
    if action == "nothing":
        return
    if action == "wait":
        return
    assert action == "dispatch", action
    schedule["window_seen_open_at"] = window[0] if window is not None else None
    reason = dispatch_scheduled(schedule, run, agents())
    if reason is None:
        schedule.update({"marked_at": now, "pending_skip_reason": "", "next_attempt_at": None})
    else:
        # A create that failed may have left a chat behind, so it is not retried every minute.
        backoff = RETRY_BACKOFF_SECONDS_BY_SKIP.get(reason, 0)
        schedule.update({"pending_skip_reason": reason, "next_attempt_at": now + backoff if backoff else None})
    write_json(SCHEDULE_PATH, schedule)


def dispatch_scheduled(schedule, run, agents):
    """Start the scheduled run, marking ``schedule`` with its chat; the skip reason when it could not start."""
    if is_run_in_flight(run, agents):
        return SKIP_UPDATE_IN_FLIGHT
    if is_update_lease_held():
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




def decide_watchdog_action(run, agents, busy_by_name, watchdog_state, now):
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
    if is_agent_busy(lead, busy_by_name) or is_agent_busy(worker, busy_by_name):
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
    """Record STUCK on the run through the template's own recorder, writing the record directly where it has none."""
    lead_name = str(run.get("chat_agent_name"))
    recorded = run_command(
        [
            sys.executable or "python3",
            UPDATE_SELF_SCRIPT,
            "run-status",
            "verdict",
            "STUCK",
            "--chat",
            lead_name,
            "--detail",
            detail,
        ],
        SHORT_COMMAND_TIMEOUT_SECONDS,
        agent_env(),
    )
    if recorded.returncode == 0:
        return
    log("the template's run-status recorder failed (%s); writing the verdict directly" % bounded(recorded.stderr))
    current = read_json(RUN_PATH) or dict(run)
    now = time.time()
    current.update(
        {
            "verdict": "STUCK",
            "detail": detail,
            "verdict_at": now,
            "updated_at": now,
            "is_holding": False,
            "hold_detail": "",
            "worker_agent_name": None,
        }
    )
    write_json(RUN_PATH, current)


def release_leases_of(lead):
    """Close the update leases the gone lead created, matched by the creator tk stamped or the lease's own words."""
    owners = {str(lead.get("name") or ""), str(lead.get("id") or ""), chat_id_of(lead)} - {""} if lead else set()
    tk = shutil.which("tk", path=agent_env().get("PATH")) or "system/vendor/tk/ticket"
    for lease_id, title, front, body in open_update_leases():
        is_owned = front.get("agent") in owners or any(("Held by %s " % owner) in body for owner in owners)
        if not is_owned:
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
    record_stuck_verdict(run, MESSAGE_PARTS["gone_lead_detail"])
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
    action = decide_watchdog_action(run, agents, busy_by_agent_name() if agents is not None else None, state, now)
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
    tick_watchdog(context["now"])
    tick_schedule(context["now"])


'''

UPDATE_DISPATCHER_PROGRAM_CORE: Final[str] = build_dispatcher_program_core(_DISPATCHER_PROGRAM_CORE_SOURCE)
