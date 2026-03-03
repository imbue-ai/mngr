import json
import os
import shlex
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
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


class ConversationInfo(FrozenModel):
    """Information about a conversation."""

    conversation_id: str = Field(description="Unique conversation identifier")
    model: str = Field(description="Model used for this conversation")
    created_at: str = Field(description="When the conversation was created")
    updated_at: str = Field(description="When the conversation was last updated")


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
def _build_conversation_event_paths(
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> tuple[Path, Path]:
    """Build paths to conversation and message event files for an agent."""
    agent_state_dir = host.host_dir / "agents" / str(agent.id)
    conversations_path = agent_state_dir / "events" / "conversations" / "events.jsonl"
    messages_path = agent_state_dir / "events" / "messages" / "events.jsonl"
    return conversations_path, messages_path


# Remote Python script that reads conversation and message event files and outputs
# a JSON array of conversation info objects sorted by updated_at descending.
# Parameterized by {conv_file} and {msg_file} paths.
_LIST_CONVERSATIONS_SCRIPT_TEMPLATE: str = """
import json, sys
from pathlib import Path

conv_file = Path('{conv_file}')
msg_file = Path('{msg_file}')

if not conv_file.exists():
    print('[]')
    sys.exit(0)

convs = {{}}
for line in conv_file.read_text().splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
        cid = event['conversation_id']
        convs[cid] = event
    except (json.JSONDecodeError, KeyError):
        continue

if not convs:
    print('[]')
    sys.exit(0)

updated_at = {{}}
for cid, event in convs.items():
    updated_at[cid] = event.get('timestamp', '')

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

result = []
for cid, event in convs.items():
    result.append({{
        'conversation_id': cid,
        'model': event.get('model', '?'),
        'created_at': event.get('timestamp', '?'),
        'updated_at': updated_at.get(cid, event.get('timestamp', '?')),
    }})

result.sort(key=lambda r: r['updated_at'], reverse=True)
print(json.dumps(result))
"""


def _build_remote_chat_script(
    host_dir: Path,
    agent: AgentInterface,
    host: OnlineHostInterface,
    chat_args: list[str],
) -> str:
    """Build a shell script to run chat.sh on a remote host via SSH.

    Sets the required environment variables and then execs chat.sh.
    """
    chat_script = _build_chat_script_path(host_dir)
    env_vars = _build_chat_env_vars(agent, host)

    # Build the shell command that sets env vars and runs chat.sh
    export_statements = "; ".join(f"export {key}='{value}'" for key, value in env_vars.items())
    escaped_args = " ".join(shlex.quote(arg) for arg in chat_args)
    return f"{export_statements}; exec '{chat_script}' {escaped_args}"


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
        remote_script = _build_remote_chat_script(host.host_dir, agent, host, chat_args)
        ssh_args.extend(["-t", "bash -c " + shlex.quote(remote_script)])

        logger.debug("Running SSH chat command: {}", ssh_args)
        completed = run_interactive_subprocess(ssh_args)
        if completed.returncode != 0:
            logger.debug("SSH chat session ended with exit code {}", completed.returncode)


def list_conversations_on_agent(
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> list[ConversationInfo]:
    """List conversations for an agent by reading event files on the host.

    Executes a Python script on the host that reads the conversation and message
    event JSONL files and returns a JSON array sorted by updated_at descending.
    """
    conversations_path, messages_path = _build_conversation_event_paths(agent, host)

    read_script = _LIST_CONVERSATIONS_SCRIPT_TEMPLATE.format(
        conv_file=conversations_path,
        msg_file=messages_path,
    )

    result = host.execute_command(
        f"python3 -c {shlex.quote(read_script)}",
        cwd=agent.work_dir,
    )

    if not result.success:
        logger.warning("Failed to list conversations: {}", result.stderr)
        return []

    try:
        raw_conversations = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        logger.warning("Failed to parse conversation list output: {}", result.stdout)
        return []

    return [ConversationInfo.model_validate(conv) for conv in raw_conversations]


def get_latest_conversation_id(
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> str | None:
    """Get the most recently updated conversation ID for an agent."""
    conversations = list_conversations_on_agent(agent, host)
    if not conversations:
        return None
    # Conversations are already sorted by updated_at descending
    return conversations[0].conversation_id
