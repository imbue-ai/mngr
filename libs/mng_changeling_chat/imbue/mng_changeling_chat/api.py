import os
import shlex
from pathlib import Path

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mng.api.connect import build_ssh_base_args
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.errors import NestedTmuxError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.utils.interactive_subprocess import run_interactive_subprocess


class ChatCommandError(MngError):
    """Raised when the chat command fails."""

    ...


@pure
def _build_chat_env_vars(
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> dict[str, str]:
    """Build the environment variables needed by chat.sh."""
    agent_state_dir = host.host_dir / "agents" / str(agent.id)
    return {
        "MNG_HOST_DIR": str(host.host_dir),
        "MNG_AGENT_STATE_DIR": str(agent_state_dir),
        "MNG_AGENT_WORK_DIR": str(agent.work_dir),
        "MNG_AGENT_ID": str(agent.id),
        "MNG_AGENT_NAME": str(agent.name),
    }


@pure
def _build_chat_script_path(host_dir: Path) -> str:
    """Build the path to the chat.sh script on the host."""
    return str(host_dir / "commands" / "chat.sh")


@pure
def build_chat_command_args(
    chat_mode: str,
    conversation_id: str | None,
) -> list[str]:
    """Build the arguments to pass to chat.sh based on the mode.

    Modes:
    - "new": start a new conversation
    - "last": resume the most recently updated conversation
    - "list": list all conversations
    - "resume": resume a specific conversation by ID
    """
    match chat_mode:
        case "new":
            return ["--new"]
        case "last":
            # We handle "last" by listing conversations and picking the first one
            # (they are sorted by updated_at descending by chat.sh --list)
            # This is handled in the CLI layer before calling into this module,
            # so this case should not be reached directly.
            raise ChatCommandError("'last' mode should be resolved before calling build_chat_command_args")
        case "list":
            return ["--list"]
        case "resume":
            if conversation_id is None:
                raise ChatCommandError("conversation_id is required for resume mode")
            return ["--resume", conversation_id]
        case _:
            raise ChatCommandError(f"Unknown chat mode: {chat_mode}")


def _build_remote_chat_script(
    host_dir: Path,
    agent: AgentInterface,
    chat_args: list[str],
) -> str:
    """Build a shell script to run chat.sh on a remote host via SSH.

    Sets the required environment variables and then execs chat.sh.
    """
    chat_script = _build_chat_script_path(host_dir)
    agent_state_dir = host_dir / "agents" / str(agent.id)

    # Build the shell command that sets env vars and runs chat.sh
    escaped_args = " ".join(shlex.quote(arg) for arg in chat_args)
    return (
        f"export MNG_HOST_DIR='{host_dir}'; "
        f"export MNG_AGENT_STATE_DIR='{agent_state_dir}'; "
        f"export MNG_AGENT_WORK_DIR='{agent.work_dir}'; "
        f"export MNG_AGENT_ID='{agent.id}'; "
        f"export MNG_AGENT_NAME='{agent.name}'; "
        f"exec '{chat_script}' {escaped_args}"
    )


def run_chat_on_agent(
    agent: AgentInterface,
    host: OnlineHostInterface,
    mng_ctx: MngContext,
    chat_args: list[str],
    is_unknown_host_allowed: bool,
) -> None:
    """Run the chat command on an agent, either locally or via SSH.

    For local agents, replaces the current process with the chat script.
    For remote agents, runs SSH interactively with the chat script.
    """
    logger.info("Starting chat session...")

    if host.is_local:
        chat_script = _build_chat_script_path(host.host_dir)

        if not Path(chat_script).exists():
            raise ChatCommandError(
                f"Chat script not found at {chat_script}. Is this agent a changeling with chat support?"
            )

        # Build environment with the required MNG_ variables
        env = dict(os.environ)
        env.update(_build_chat_env_vars(agent, host))

        # Handle nested tmux (chat.sh may call llm live-chat which is interactive)
        if os.environ.get("TMUX"):
            if not mng_ctx.config.is_nested_tmux_allowed:
                raise NestedTmuxError(f"{mng_ctx.config.prefix}{agent.name}")
            env.pop("TMUX", None)

        argv = [chat_script] + chat_args
        os.execvpe(chat_script, argv, env)
    else:
        ssh_args = build_ssh_base_args(host, is_unknown_host_allowed=is_unknown_host_allowed)

        # Build the remote command script
        remote_script = _build_remote_chat_script(host.host_dir, agent, chat_args)
        ssh_args.extend(["-t", "bash -c " + shlex.quote(remote_script)])

        logger.debug("Running SSH chat command: {}", ssh_args)
        completed = run_interactive_subprocess(ssh_args)
        if completed.returncode != 0:
            logger.debug("SSH chat session ended with exit code {}", completed.returncode)


def get_latest_conversation_id(
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> str | None:
    """Get the most recently updated conversation ID for an agent.

    Reads the conversations and messages event files to find the conversation
    with the most recent activity.
    """
    agent_state_dir = host.host_dir / "agents" / str(agent.id)
    conversations_events_path = agent_state_dir / "events" / "conversations" / "events.jsonl"
    messages_events_path = agent_state_dir / "events" / "messages" / "events.jsonl"

    # Build a Python script that reads the event files and outputs the latest conversation ID
    read_script = f"""
import json, sys
from pathlib import Path

conv_file = Path('{conversations_events_path}')
msg_file = Path('{messages_events_path}')

if not conv_file.exists():
    sys.exit(1)

convs = {{}}
for line in conv_file.read_text().splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
        cid = event['conversation_id']
        convs[cid] = event.get('timestamp', '')
    except (json.JSONDecodeError, KeyError):
        continue

if not convs:
    sys.exit(1)

# Check messages for latest activity
updated_at = dict(convs)
if msg_file.exists():
    for line in msg_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            cid = msg.get('conversation_id', '')
            ts = msg.get('timestamp', '')
            if cid in convs and ts:
                if cid not in updated_at or ts > updated_at[cid]:
                    updated_at[cid] = ts
        except (json.JSONDecodeError, KeyError):
            continue

latest_cid = max(updated_at, key=lambda c: updated_at[c])
print(latest_cid)
"""

    result = host.execute_command(
        f"python3 -c {shlex.quote(read_script)}",
        cwd=agent.work_dir,
    )

    if result.success and result.stdout.strip():
        return result.stdout.strip()
    return None
