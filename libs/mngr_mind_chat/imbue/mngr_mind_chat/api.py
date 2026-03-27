import json
import os
import shlex
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.api.connect import build_ssh_base_args
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.utils.env_utils import build_source_env_shell_commands
from imbue.mngr.utils.env_utils import parse_env_file
from imbue.mngr.utils.interactive_subprocess import run_interactive_subprocess


class ChatCommandError(MngrError):
    """Raised when the chat command fails."""

    ...


class ConversationInfo(FrozenModel):
    """Information about a conversation."""

    conversation_id: str = Field(description="Unique conversation identifier")
    name: str = Field(default="", description="Human-readable conversation name from tags")
    model: str = Field(description="Model used for this conversation")
    created_at: str = Field(description="When the conversation was created")
    updated_at: str = Field(description="When the conversation was last updated")


@pure
def get_agent_state_dir(
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> Path:
    """Get the agent's state directory on the host."""
    return host.host_dir / "agents" / str(agent.id)


@pure
def _build_chat_env_vars(
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> dict[str, str]:
    """Build the environment variables needed by chat.sh."""
    agent_state_dir = get_agent_state_dir(agent, host)
    return {
        "MNGR_HOST_DIR": str(host.host_dir),
        "MNGR_AGENT_STATE_DIR": str(agent_state_dir),
        "MNGR_AGENT_WORK_DIR": str(agent.work_dir),
        "MNGR_AGENT_ID": str(agent.id),
        "MNGR_AGENT_NAME": str(agent.name),
    }


@pure
def _build_env_file_paths(
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> tuple[Path, Path]:
    """Build paths to the host and agent env files."""
    host_env_path = host.host_dir / "env"
    agent_env_path = host.get_agent_env_path(agent)
    return host_env_path, agent_env_path


def _load_env_file_into_dict(env_path: Path, env: dict[str, str]) -> None:
    """Parse an env file and add its variables to the dict.

    Uses the shared parse_env_file utility (backed by python-dotenv).
    """
    if not env_path.exists():
        return
    parsed = parse_env_file(env_path.read_text())
    env.update(parsed)


@pure
def _build_chat_script_path(
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> str:
    agent_state_dir = get_agent_state_dir(agent, host)
    return str(agent_state_dir / "commands" / "chat.sh")


@pure
def _build_conversation_db_and_messages_paths(
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> tuple[Path, Path]:
    """Build paths to the llm database and message event file for an agent."""
    agent_state_dir = get_agent_state_dir(agent, host)
    db_path = agent_state_dir / "llm_data" / "logs.db"
    messages_path = agent_state_dir / "events" / "messages" / "events.jsonl"
    return db_path, messages_path


# Remote Python script that reads conversations from the mind_conversations
# table in the llm sqlite database and message events from a JSONL file, then
# outputs a JSON array of conversation info objects sorted by updated_at descending.
# Receives paths as sys.argv[1] (db) and sys.argv[2] (messages).
_LIST_CONVERSATIONS_SCRIPT: str = """
import json, sqlite3, sys
from pathlib import Path

db_path = Path(sys.argv[1])
msg_file = Path(sys.argv[2])

if not db_path.exists():
    print('[]')
    sys.exit(0)

conversations = {}
try:
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        rows = conn.execute(
            'SELECT cc.conversation_id, c.model, cc.created_at, cc.tags '
            'FROM mind_conversations cc '
            'LEFT JOIN conversations c ON cc.conversation_id = c.id'
        ).fetchall()
    finally:
        conn.close()
    for conversation_id, model, created_at, tags_json in rows:
        try:
            tags = json.loads(tags_json) if tags_json else {}
        except json.JSONDecodeError as e:
            sys.stderr.write(f'WARNING: malformed tags JSON for {conversation_id}: {e}\\n')
            tags = {}
        conversations[conversation_id] = {
            'conversation_id': conversation_id,
            'model': model or '?',
            'created_at': created_at or '',
            'updated_at': created_at or '',
            'name': tags.get('name', ''),
        }
except sqlite3.Error as e:
    print(f'WARNING: failed to read conversations from database: {e}', file=sys.stderr)

if not conversations:
    print('[]')
    sys.exit(0)

if msg_file.exists():
    for line_idx, line in enumerate(msg_file.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            conversation_id = msg.get('conversation_id', '')
            ts = msg.get('timestamp', '')
            if conversation_id in conversations and ts:
                if ts > conversations[conversation_id]['updated_at']:
                    conversations[conversation_id]['updated_at'] = ts
        except (json.JSONDecodeError, KeyError) as e:
            print(f'WARNING: skipping malformed message event line {line_idx}: {e}', file=sys.stderr)
            continue

result = sorted(conversations.values(), key=lambda r: r['updated_at'], reverse=True)
print(json.dumps(result))
"""


@pure
def _build_remote_chat_script(
    agent: AgentInterface,
    host: OnlineHostInterface,
    chat_args: list[str],
) -> str:
    """Build a shell script to run chat.sh on a remote host via SSH.

    Sources the host and agent env files (for API keys etc.), sets the
    required MNGR_ environment variables, and then execs chat.sh.
    """
    chat_script = _build_chat_script_path(agent, host)
    env_vars = _build_chat_env_vars(agent, host)
    host_env_path, agent_env_path = _build_env_file_paths(agent, host)

    # Source env files first (for API keys, etc.), then set MNGR_ vars
    source_commands = build_source_env_shell_commands(host_env_path, agent_env_path)
    source_prefix = "; ".join(source_commands) + "; "

    # Use shlex.quote for each value to prevent shell injection from
    # agent names or paths containing special characters
    export_statements = "; ".join(f"export {key}={shlex.quote(value)}" for key, value in env_vars.items())
    escaped_args = " ".join(shlex.quote(arg) for arg in chat_args)
    return f"{source_prefix}{export_statements}; exec {shlex.quote(chat_script)} {escaped_args}"


def run_chat_on_agent(  # pragma: no cover
    agent: AgentInterface,
    host: OnlineHostInterface,
    mngr_ctx: MngrContext,
    chat_args: list[str],
    is_unknown_host_allowed: bool,
) -> None:
    """Run the chat command on an agent, either locally or via SSH.

    For local agents, replaces the current process with the chat script.
    For remote agents, runs SSH interactively with the chat script.
    """
    logger.info("Starting chat session...")

    if host.is_local:
        chat_script = _build_chat_script_path(agent, host)

        if not Path(chat_script).exists():
            raise ChatCommandError(f"Chat script not found at {chat_script}. Is this agent a mind with chat support?")

        # Build environment: start with current env, source host/agent env files
        # (for API keys etc.), then overlay the MNGR_ variables
        env = dict(os.environ)
        host_env_path, agent_env_path = _build_env_file_paths(agent, host)
        _load_env_file_into_dict(host_env_path, env)
        _load_env_file_into_dict(agent_env_path, env)
        env.update(_build_chat_env_vars(agent, host))

        argv = [chat_script] + chat_args
        os.execvpe(chat_script, argv, env)
    else:
        ssh_args = build_ssh_base_args(host, is_unknown_host_allowed=is_unknown_host_allowed)

        # Build the remote command script (sources env files + runs chat.sh)
        remote_script = _build_remote_chat_script(agent, host, chat_args)
        ssh_args.extend(["-t", "bash -c " + shlex.quote(remote_script)])

        logger.debug("Running SSH chat command: {}", ssh_args)
        completed = run_interactive_subprocess(ssh_args)
        if completed.returncode != 0:
            logger.debug("SSH chat session ended with exit code {}", completed.returncode)


def list_conversations_on_agent(
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> list[ConversationInfo]:
    """List conversations for an agent by querying the llm database on the host.

    Executes a Python script on the host that reads the mind_conversations
    table from the llm sqlite database and message events from JSONL, returning
    a JSON array sorted by updated_at descending.

    Raises ChatCommandError if the remote command fails or returns unparseable output.
    """
    db_path, messages_path = _build_conversation_db_and_messages_paths(agent, host)

    # Pass paths as command-line arguments (not string interpolation) to avoid
    # injection issues from paths containing special characters
    command = (
        f"python3 -c {shlex.quote(_LIST_CONVERSATIONS_SCRIPT)}"
        f" {shlex.quote(str(db_path))}"
        f" {shlex.quote(str(messages_path))}"
    )

    result = host.execute_idempotent_command(
        command,
        cwd=agent.work_dir,
    )

    if not result.success:
        raise ChatCommandError(f"Failed to list conversations for agent {agent.name}: {result.stderr}")

    try:
        raw_conversations = json.loads(result.stdout.strip())
    except json.JSONDecodeError as e:
        raise ChatCommandError(f"Failed to parse conversation list for agent {agent.name}: {result.stdout}") from e

    return [ConversationInfo.model_validate(conv) for conv in raw_conversations]


def get_latest_conversation_id(
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> str | None:
    """Get the most recently updated conversation ID for an agent.

    Raises ChatCommandError if conversations cannot be listed.
    """
    conversations = list_conversations_on_agent(agent, host)
    if not conversations:
        return None
    # Conversations are already sorted by updated_at descending
    return conversations[0].conversation_id
