import os
import shlex
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.api.data_types import ConnectionOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import NestedTmuxError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.utils.interactive_subprocess import run_interactive_subprocess

# Exit codes used by the remote SSH wrapper script to signal post-disconnect actions.
# These are checked by connect_to_agent after the SSH session ends to determine
# whether to destroy or stop the agent locally.
SIGNAL_EXIT_CODE_DESTROY: Final[int] = 10
SIGNAL_EXIT_CODE_STOP: Final[int] = 11


def build_post_attach_resize_script(session_name: str) -> str:
    """Build a shell command that resizes tmux windows and sends SIGWINCH.

    After a tmux client attaches, resize all windows to match the client
    (resize-window -A), then explicitly send SIGWINCH to each pane's child
    processes. The explicit SIGWINCH is needed because resize-window -A can
    be a no-op (and thus not trigger SIGWINCH) when the window already
    matches the client size (e.g., due to window-size=latest).

    Uses pgrep -P to find child processes of each pane. This avoids any
    dependency on matching the agent's process name, which is unreliable
    (on macOS, Claude's process title shows as its version number rather
    than "claude").
    """
    return (
        f"tmux list-windows -t '{session_name}' -F '#I' | "
        f"xargs -I{{}} tmux resize-window -t '{session_name}':{{}} -A; "
        f"tmux list-panes -t '{session_name}' -F '#{{pane_pid}}' | "
        f"xargs -I{{}} sh -c 'kill -WINCH {{}} $(pgrep -P {{}})' 2>/dev/null"
    )


def _build_ssh_activity_wrapper_script(session_name: str, host_dir: Path) -> str:
    """Build a shell script that tracks SSH activity while running tmux.

    The script:
    1. Creates the activity directory if needed
    2. Starts a background loop that writes JSON activity to activity/ssh
    3. Runs tmux attach (foreground, blocking)
    4. Kills the activity tracker when tmux exits
    5. Checks for signal files (written by tmux Ctrl-q/Ctrl-t bindings) and
       exits with a specific code to tell the local mngr process what to do

    The activity file contains JSON with:
    - time: milliseconds since Unix epoch (int)
    - ssh_pid: the PID of the SSH activity tracker process (for debugging)

    Note: The authoritative activity time is the file's mtime, not the JSON content.
    """
    activity_dir = host_dir / "activity"
    activity_file = activity_dir / "ssh"
    signal_file = host_dir / "signals" / session_name
    # Use single quotes around most things to avoid shell expansion issues,
    # but the paths need to be interpolated
    return (
        f"mkdir -p '{activity_dir}'; "
        f"(while true; do "
        f"TIME_MS=$(($(date +%s) * 1000)); "
        f'printf \'{{\\n  "time": %d,\\n  "ssh_pid": %d\\n}}\\n\' "$TIME_MS" "$$" > \'{activity_file}\'; '
        f"sleep 5; done) & "
        "MNGR_ACTIVITY_PID=$!; "
        # Force a terminal resize after attaching to trigger SIGWINCH delivery.
        f"(sleep 3; {build_post_attach_resize_script(session_name)}) 2>/dev/null & "
        # actually attach
        f"tmux attach -t '{session_name}'; "
        "kill $MNGR_ACTIVITY_PID 2>/dev/null; "
        # Check for signal files written by tmux key bindings (Ctrl-q writes "destroy", Ctrl-t writes "stop")
        f"SIGNAL_FILE='{signal_file}'; "
        'if [ -f "$SIGNAL_FILE" ]; then '
        'ACTION=$(cat "$SIGNAL_FILE"); '
        'rm -f "$SIGNAL_FILE"; '
        f'if [ "$ACTION" = "destroy" ]; then exit {SIGNAL_EXIT_CODE_DESTROY}; '
        f'elif [ "$ACTION" = "stop" ]; then exit {SIGNAL_EXIT_CODE_STOP}; fi; '
        "fi"
    )


def build_ssh_base_args(
    host: OnlineHostInterface,
    is_unknown_host_allowed: bool = False,
) -> list[str]:
    """Build base SSH command args for connecting to a remote host.

    Returns args like ["ssh", "-i", key, "-p", port, "-o", ..., "user@host"].
    The caller appends the remote command or other options (e.g., -t).

    Raises MngrError if no known_hosts file is configured and
    is_unknown_host_allowed is False.
    """
    pyinfra_host = host.connector.host
    ssh_host = pyinfra_host.name
    ssh_user = pyinfra_host.data.get("ssh_user")
    ssh_port = pyinfra_host.data.get("ssh_port")
    ssh_key = pyinfra_host.data.get("ssh_key")
    ssh_known_hosts_file = pyinfra_host.data.get("ssh_known_hosts_file")

    ssh_args = ["ssh"]

    if ssh_key:
        ssh_args.extend(["-i", str(ssh_key)])

    if ssh_port:
        ssh_args.extend(["-p", str(ssh_port)])

    # Use the known_hosts file if provided (for pre-trusted host keys)
    if ssh_known_hosts_file and ssh_known_hosts_file != "/dev/null":
        ssh_args.extend(["-o", f"UserKnownHostsFile={ssh_known_hosts_file}"])
        ssh_args.extend(["-o", "StrictHostKeyChecking=yes"])
    elif is_unknown_host_allowed:
        ssh_args.extend(["-o", "StrictHostKeyChecking=no"])
        ssh_args.extend(["-o", "UserKnownHostsFile=/dev/null"])
    else:
        raise MngrError("No known_hosts file is configured for this host. Cannot establish a secure SSH connection.")

    target = f"{ssh_user}@{ssh_host}" if ssh_user else ssh_host
    ssh_args.append(target)

    return ssh_args


def _build_ssh_args(
    host: OnlineHostInterface,
    connection_opts: ConnectionOptions,
) -> list[str]:
    """Build the SSH command arguments for connecting to a remote host.

    Delegates to build_ssh_base_args, passing through the is_unknown_host_allowed
    option from connection_opts.
    """
    return build_ssh_base_args(host, is_unknown_host_allowed=connection_opts.is_unknown_host_allowed)


@pure
def _determine_post_disconnect_action(
    exit_code: int,
    session_name: str,
) -> tuple[str, list[str]] | None:
    """Given an SSH exit code, return the post-disconnect action to execute.

    Returns (executable, argv) if an action should be taken, or None if no
    action is needed (normal exit or unknown exit code).
    """
    if exit_code == SIGNAL_EXIT_CODE_DESTROY:
        return ("mngr", ["mngr", "destroy", "--session", session_name, "-f"])
    elif exit_code == SIGNAL_EXIT_CODE_STOP:
        return ("mngr", ["mngr", "stop", "--session", session_name])
    else:
        return None


def resolve_connect_command(
    cli_connect_command: str | None,
    mngr_ctx: MngrContext,
) -> str | None:
    """Resolve the connect command from a CLI option or global config."""
    if cli_connect_command is not None:
        return cli_connect_command
    return mngr_ctx.config.connect_command


def run_connect_command(
    connect_command: str,
    agent_name: str,
    session_name: str,
    is_local: bool,
) -> None:
    """Run a custom connect command instead of the builtin connect_to_agent.

    Sets environment variables so the command can reference the agent,
    then replaces the current process.
    """
    env = dict(os.environ)
    env["MNGR_AGENT_NAME"] = agent_name
    env["MNGR_SESSION_NAME"] = session_name
    env["MNGR_HOST_IS_LOCAL"] = "true" if is_local else "false"
    logger.debug("Running custom connect command: {}", connect_command)
    os.execvpe("sh", ["sh", "-c", connect_command], env)


def connect_to_agent(
    agent: AgentInterface,
    host: OnlineHostInterface,
    mngr_ctx: MngrContext,
    connection_opts: ConnectionOptions,
) -> None:
    """Connect to an agent via tmux attach (local) or SSH + tmux attach (remote).

    For local agents, replaces the current process with: tmux attach -t <session_name>

    For remote agents, runs SSH interactively and then checks the exit code to
    determine if a post-disconnect action (destroy/stop) was requested via the
    tmux key bindings (Ctrl-q for destroy, Ctrl-t for stop). If so, replaces the
    current process with the appropriate mngr command to perform the action locally.

    For local agents, this function does not return (os.execvp replaces the process).
    For remote agents, this function returns after the SSH session ends unless a
    post-disconnect action is triggered (in which case os.execvp replaces the process).
    """
    logger.info("Connecting to agent...")

    session_name = f"{mngr_ctx.config.prefix}{agent.name}"

    if host.is_local:
        # Detect nested tmux: if $TMUX is set, we're inside a tmux session
        env = os.environ
        if os.environ.get("TMUX"):
            if not mngr_ctx.config.is_nested_tmux_allowed:
                raise NestedTmuxError(session_name)
            # Copy and remove TMUX so tmux allows the nested attachment
            env = dict(os.environ)
            del env["TMUX"]
        os.execvpe("tmux", ["tmux", "attach", "-t", session_name], env)
    else:
        ssh_args = _build_ssh_args(host, connection_opts)

        # Build wrapper script that tracks SSH activity while running tmux
        wrapper_script = _build_ssh_activity_wrapper_script(session_name, host.host_dir)
        # Pass the wrapper as a single remote command string so SSH doesn't
        # split it into separate words. SSH concatenates multiple remote command
        # arguments with spaces, which would cause 'bash -c' to only receive
        # the first word of the script (e.g., 'mkdir') instead of the full script.
        ssh_args.extend(["-t", "bash -c " + shlex.quote(wrapper_script)])

        # hack to make this work for me, we really should be more principled about this...
        fixed_env = {**os.environ}
        if fixed_env.get("TERM") == "xterm-kitty":
            fixed_env["TERM"] = "xterm-256color"

        # Use run_interactive_subprocess instead of os.execvp so we can check the exit code
        # and run post-disconnect actions (destroy/stop) triggered by tmux key bindings
        completed = run_interactive_subprocess(ssh_args, env=fixed_env)
        exit_code = completed.returncode

        action = _determine_post_disconnect_action(exit_code, session_name)
        if action is not None:
            executable, argv = action
            logger.info("Running post-disconnect action: {}", argv)
            os.execvp(executable, argv)
        else:
            logger.debug("SSH session ended with exit code {} (no post-disconnect action)", exit_code)
