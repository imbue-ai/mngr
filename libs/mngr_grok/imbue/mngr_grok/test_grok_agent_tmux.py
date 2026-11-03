import shutil
from pathlib import Path
from typing import Final

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.create import create
from imbue.mngr.api.providers import get_local_host
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import TransferMode
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import tmux_session_cleanup
from imbue.mngr_grok.grok_config import GROK_PLUGIN_STATE_RELATIVE_PATH
from imbue.mngr_grok.grok_config import ROOT_TURNS_RELATIVE_PATH
from imbue.mngr_grok.plugin import GrokAgent
from imbue.mngr_grok.resources.grok_hook import ACTIVE_MARKER_FILENAME
from imbue.mngr_grok.resources.grok_hook import SESSION_STARTED_FILENAME
from imbue.mngr_grok.resources.grok_hook import SUBMISSIONS_LOG_FILENAME
from imbue.mngr_grok.resources.grok_hook import normalized_prompt_tail
from imbue.mngr_grok.testing import put_executable_first_on_path

# A stand-in `grok`: one session, a composer and footer drawn the way the pinned grok draws them under tmux,
# mngr's provisioned hooks run for SessionStart, UserPromptSubmit and Stop, and a turn that stays in flight
# until `$MNGR_AGENT_STATE_DIR/standin_release_turn` exists, then writes its turn_completed record.
_STANDIN_GROK_SCRIPT: Final[str] = r"""#!/usr/bin/env python3
import json
import os
import select
import subprocess
import sys
import termios
import tty
import urllib.parse
import uuid

HOOK_EVENT_NAMES = {"session_start": "SessionStart", "user_prompt_submit": "UserPromptSubmit", "stop": "Stop"}
FOOTER_TAIL_HINTS = "Shift+Tab:mode  │  Ctrl+x:shortcuts"
BUSY_FOOTER = "Shift+Tab:mode  │  Ctrl+c:cancel  │  Ctrl+x:shortcuts"


def read_session_id(arguments):
    for flag in ("--session-id", "--resume"):
        if flag in arguments:
            return arguments[arguments.index(flag) + 1]
    raise SystemExit("the stand-in grok needs --session-id or --resume")


if sys.argv[1:] == ["--version"]:
    os.write(sys.stdout.fileno(), b"grok 1.0.62 (0000000)\n")
    raise SystemExit(0)

grok_home = os.environ["GROK_HOME"]
release_path = os.path.join(os.environ["MNGR_AGENT_STATE_DIR"], "standin_release_turn")
session_id = read_session_id(sys.argv[1:])
work_dir = os.getcwd()
bucket = urllib.parse.quote(os.path.realpath(work_dir), safe="")
session_dir = os.path.join(grok_home, "sessions", bucket, session_id)
os.makedirs(session_dir, exist_ok=True)
with open(os.path.join(session_dir, "summary.json"), "w") as summary_file:
    summary_file.write("{}\n")
updates_path = os.path.join(session_dir, "updates.jsonl")
state = {"text": "", "prompt_id": None, "event_count": 0, "is_running": True}


def append_update(update):
    state["event_count"] += 1
    meta = {"eventId": "%s-%d" % (session_id, state["event_count"])}
    record = {"method": "_x.ai/session/update", "params": {"sessionId": session_id, "update": update, "_meta": meta}}
    with open(updates_path, "a") as updates_file:
        updates_file.write(json.dumps(record) + "\n")


def run_hooks(event_name, fields):
    hooks_dir = os.path.join(grok_home, "hooks")
    with open(os.path.join(hooks_dir, "mngr.json")) as hooks_file:
        hooks_config = json.load(hooks_file)
    payload = dict(fields, hookEventName=event_name, sessionId=session_id, cwd=work_dir)
    for matcher in hooks_config["hooks"].get(HOOK_EVENT_NAMES[event_name], []):
        for hook in matcher["hooks"]:
            command_path = os.path.join(hooks_dir, hook["command"])
            subprocess.run([command_path], input=json.dumps(payload).encode(), cwd=work_dir, check=True, timeout=30)


def render():
    columns, rows = os.get_terminal_size(sys.stdout.fileno())
    is_busy = state["prompt_id"] is not None
    text = state["text"]
    if text:
        footer = ("Enter:queue" if is_busy else "Enter:send") + "  │  Opt+Enter:newline  │  " + FOOTER_TAIL_HINTS
    elif is_busy:
        footer = BUSY_FOOTER
    else:
        footer = FOOTER_TAIL_HINTS
    box_width = columns - 6
    lines = [""] * (rows - 7)
    lines.append("    ⠋ Responding… 1.0s" if is_busy else "")
    lines.append("")
    lines.append("  ╭" + "─" * box_width + "╮")
    lines.append("  │ ❯ " + text[: box_width - 3].ljust(box_width - 3) + "│")
    lines.append("  ╰" + "─" * box_width + "╯")
    lines.append("")
    lines.append("  " + footer)
    os.write(sys.stdout.fileno(), ("\x1b[H\x1b[2J" + "\r\n".join(lines)).encode())


def submit():
    prompt_id = str(uuid.uuid4())
    prompt = state["text"]
    state["text"] = ""
    append_update({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": prompt}})
    run_hooks("user_prompt_submit", {"promptId": prompt_id, "prompt": prompt})
    state["prompt_id"] = prompt_id


def finish_turn():
    os.remove(release_path)
    prompt_id = state["prompt_id"]
    append_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "stand-in reply"}})
    run_hooks("stop", {"promptId": prompt_id, "reason": "end_turn", "stopHookActive": False, "backgroundTasks": []})
    append_update({"sessionUpdate": "turn_completed", "prompt_id": prompt_id, "stop_reason": "end_turn"})
    state["prompt_id"] = None


def handle_key(character):
    if character in ("\r", "\n"):
        if state["text"] and state["prompt_id"] is None:
            submit()
    elif character == "\x7f":
        state["text"] = state["text"][:-1]
    elif character == "\x11":
        state["is_running"] = False
    elif character.isprintable():
        state["text"] += character
    else:
        pass


terminal_fd = sys.stdin.fileno()
saved_terminal_attributes = termios.tcgetattr(terminal_fd)
tty.setraw(terminal_fd)
try:
    run_hooks("session_start", {"source": "new"})
    render()
    while state["is_running"]:
        readable, _, _ = select.select([terminal_fd], [], [], 0.1)
        if readable:
            for character in os.read(terminal_fd, 4096).decode("utf-8", "replace"):
                handle_key(character)
            render()
        if state["prompt_id"] is not None and os.path.exists(release_path):
            finish_turn()
            render()
finally:
    termios.tcsetattr(terminal_fd, termios.TCSADRAIN, saved_terminal_attributes)
"""

_MESSAGE: Final[str] = "hello from the grok stand-in 7731"


@pytest.mark.tmux
def test_grok_agent_confirms_a_message_and_reports_its_turn(
    temp_mngr_ctx: MngrContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_executable_first_on_path(tmp_path / "standin-bin", monkeypatch, "grok", _STANDIN_GROK_SCRIPT)
    bash_path = shutil.which("bash")
    assert bash_path is not None
    monkeypatch.setenv("SHELL", bash_path)
    monkeypatch.setenv("MNGR_GROK_SUPERVISOR_POLL_SECONDS", "1")
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    mngr_ctx = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().is_auto_approve, True))
    local_host = get_local_host(mngr_ctx)
    agent_name = AgentName(f"grok-standin-{get_short_random_string()}")

    with tmux_session_cleanup(f"{mngr_ctx.config.prefix}{agent_name}"):
        result = create(
            source_location=HostLocation(host=local_host, path=work_dir),
            target_host=local_host,
            agent_options=CreateAgentOptions(
                name=agent_name, agent_type=AgentTypeName("grok"), transfer_mode=TransferMode.NONE
            ),
            mngr_ctx=mngr_ctx,
        )
        agent = result.agent
        assert isinstance(agent, GrokAgent)
        state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)
        assert (state_dir / SESSION_STARTED_FILENAME).is_file()
        assert not (state_dir / ACTIVE_MARKER_FILENAME).exists()

        with capture_loguru(level="DEBUG") as log_output:
            agent.send_message(_MESSAGE)

        assert "Message submitted successfully (confirmed by submissions-log)" in log_output.getvalue()
        assert (
            f" {normalized_prompt_tail(_MESSAGE)}\n"
            in (state_dir / GROK_PLUGIN_STATE_RELATIVE_PATH / SUBMISSIONS_LOG_FILENAME).read_text()
        )
        assert (state_dir / ACTIVE_MARKER_FILENAME).is_file()

        (state_dir / "standin_release_turn").touch()

        wait_for(
            lambda: not (state_dir / ACTIVE_MARKER_FILENAME).exists(),
            timeout=30.0,
            error_message="the grok turn never ended: the active marker is still present",
        )
        assert '"turn_completed"' in (state_dir / "logs" / "grok_transcript" / "events.jsonl").read_text()
        assert list((state_dir / ROOT_TURNS_RELATIVE_PATH).iterdir()) == []
