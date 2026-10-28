import os
import shlex
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.imbue_common.suspension import ClockReading
from imbue.imbue_common.suspension import read_clocks
from imbue.imbue_common.suspension import was_suspended_since
from imbue.mngr.api.data_types import ConnectionOptions
from imbue.mngr.api.find import ensure_agent_started
from imbue.mngr.api.find import ensure_host_started
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostOfflineError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import NestedTmuxError
from imbue.mngr.hosts.tmux import TmuxSessionTarget
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.utils.deps import SSH
from imbue.mngr.utils.duration import parse_duration_to_seconds
from imbue.mngr.utils.interactive_subprocess import run_interactive_subprocess
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.ssh import quote_ssh_option_value

# Exit codes used by the remote SSH wrapper script to signal post-disconnect actions.
# These are checked by connect_to_agent after the SSH session ends to determine
# whether to destroy or stop the agent locally.
SIGNAL_EXIT_CODE_DESTROY: Final[int] = 10
SIGNAL_EXIT_CODE_STOP: Final[int] = 11

# ssh's own exit status for an error of its own, such as a lost or refused connection.
SSH_ERROR_EXIT_CODE: Final[int] = 255

# The modes tmux's client switches off when it releases a terminal (tmux tty.c, tty_stop_tty).
# Saving and restoring the cursor around the scroll-region reset undoes the cursor home it does.
# ?1049h before ?1049l makes leaving the alternate screen a no-op when it is not active.
TERMINAL_MODE_RESET: Final[bytes] = (
    b"\x1b7\x1b[r\x1b8"
    b"\x1b[?1049h\x1b[?1049l"
    b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1005l"
    b"\x1b[?2004l"
    b"\x1b[?1004l"
    b"\x1b[?1l\x1b>"
    b"\x1b[m"
    b"\x1b[?25h"
)

_TMUX_CONTROL_MODE_FLAGS: Final[frozenset[str]] = frozenset({"-C", "-CC"})

_STDOUT_FD: Final[int] = 1

# Set in the environment of a custom connect command so that any nested mngr
# invocation it makes (e.g. a script that calls `mngr connect` to do the real
# attach) falls back to the builtin connect instead of re-running the custom
# command, which would recurse indefinitely.
CONNECT_COMMAND_ACTIVE_ENV_VAR: Final[str] = "MNGR_CONNECT_COMMAND_ACTIVE"


@pure
def _build_ssh_activity_wrapper_script(
    session_name: str,
    agent_id: AgentId,
    host_dir: Path,
    attach_args: tuple[str, ...] = (),
) -> str:
    """Build a shell script that tracks SSH activity while running tmux.

    The script:
    1. Creates the activity directory if needed
    2. Starts a background loop that writes JSON activity to activity/ssh
    3. Runs tmux attach (foreground, blocking)
    4. Kills the activity tracker when tmux exits
    5. Checks for signal files (written by tmux Ctrl-q/Ctrl-t bindings) and
       exits with a specific code to tell the local mngr process what to do

    The bindings name the signal file for the agent id, which a rename while attached
    leaves alone. Any signal file left from an earlier connect is removed before
    attaching, so it cannot turn a plain detach into a stop or destroy.

    The activity file contains JSON with:
    - time: milliseconds since Unix epoch (int)
    - ssh_pid: the PID of the SSH activity tracker process (for debugging)

    Note: The authoritative activity time is the file's mtime, not the JSON content.

    ``attach_args`` are tmux *client* flags spliced in before the ``attach``
    subcommand (``tmux <attach_args> attach ...``). The motivating case is
    ``-CC`` (iTerm2 control mode), which turns the SSH session's stdout into a
    control-protocol stream.

    Note: the post-attach SIGWINCH repaint nudge is no longer sent here. It now
    fires from a per-session tmux ``client-attached`` hook (set at session
    creation), so every attach -- this SSH wrapper, a plain ``tmux attach``, the
    ttyd terminal -- triggers the redraw rather than only ``mngr connect``.
    """
    activity_dir = host_dir / "activity"
    activity_file = activity_dir / "ssh"
    # CLEANUP: drop the session-name file once no host's tmux config predates the bindings
    # that name it for the agent id (every host has restarted its agents on such an mngr).
    quoted_signal_files = " ".join(
        shlex.quote(str(host_dir / "signals" / key)) for key in (str(agent_id), session_name)
    )
    # Render the shared attach argv to a shell-command string. shlex.join quotes
    # each token (matching TmuxSessionTarget.as_shell_arg for the target), so this
    # is the same command the local path execs -- just as text for the remote shell.
    attach_command = shlex.join(build_attach_argv(TmuxSessionTarget(session_name=session_name), attach_args))
    # Use single quotes around most things to avoid shell expansion issues,
    # but the paths need to be interpolated
    return (
        f"mkdir -p '{activity_dir}'; "
        f"(while true; do "
        f"TIME_MS=$(($(date +%s) * 1000)); "
        f'printf \'{{\\n  "time": %d,\\n  "ssh_pid": %d\\n}}\\n\' "$TIME_MS" "$$" > \'{activity_file}\'; '
        f"sleep 5; done) & "
        "MNGR_ACTIVITY_PID=$!; "
        f"rm -f {quoted_signal_files}; "
        # actually attach. Route the -t target through TmuxSessionTarget so the
        # = exact-match prefix and shell-escaping rule are uniform with the rest
        # of the codebase (see TmuxSessionTarget docstring for the prefix-matching
        # bug this guards against).
        f"{attach_command}; "
        "kill $MNGR_ACTIVITY_PID 2>/dev/null; "
        # Check for signal files written by tmux key bindings (Ctrl-q writes "destroy", Ctrl-t writes "stop")
        f"for SIGNAL_FILE in {quoted_signal_files}; do "
        'if [ -f "$SIGNAL_FILE" ]; then '
        'ACTION=$(cat "$SIGNAL_FILE"); '
        'rm -f "$SIGNAL_FILE"; '
        f'if [ "$ACTION" = "destroy" ]; then exit {SIGNAL_EXIT_CODE_DESTROY}; '
        f'elif [ "$ACTION" = "stop" ]; then exit {SIGNAL_EXIT_CODE_STOP}; fi; '
        "fi; done"
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
    Raises BinaryNotInstalledError if the ssh binary is not installed (ssh is an
    optional dependency, only needed to attach to remote agents).
    """
    SSH.require()

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
        ssh_args.extend(["-o", f"UserKnownHostsFile={quote_ssh_option_value(ssh_known_hosts_file)}"])
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
    agent_id: AgentId,
    host_id: HostId,
) -> tuple[str, list[str]] | None:
    """Given an SSH exit code, return the post-disconnect action to execute.

    Returns (executable, argv) if an action should be taken, or None if no
    action is needed (normal exit or unknown exit code). The agent is addressed
    as ``ID@HOST``: the same agent id can exist on more than one host, and a bare
    id would stop or destroy every instance.
    """
    agent_address = f"{agent_id}@{host_id}"
    if exit_code == SIGNAL_EXIT_CODE_DESTROY:
        return ("mngr", ["mngr", "destroy", agent_address, "-f"])
    elif exit_code == SIGNAL_EXIT_CODE_STOP:
        return ("mngr", ["mngr", "stop", agent_address])
    else:
        return None


@pure
def build_attach_argv(target: TmuxSessionTarget, attach_args: tuple[str, ...]) -> list[str]:
    """Build the argv for ``tmux [<attach_args>] attach -t <target>``.

    The single place that knows the shape of the attach command, so no call site
    can forget to thread the client flags. ``attach_args`` are tmux *client*
    flags and so are spliced in before the ``attach`` subcommand (e.g. ``-CC``
    for iTerm2 control mode yields ``tmux -CC attach -t =<session>``).

    For a local attach the list is handed straight to the tmux exec call; for the
    remote SSH wrapper it is rendered to a shell-command string via ``shlex.join``.
    The target goes through :class:`TmuxSessionTarget` (its raw ``as_target_arg``,
    since argv bypasses the shell) so the exact-match ``=`` rule is not hand-rolled.
    """
    return ["tmux", *attach_args, "attach", "-t", target.as_target_arg()]


def resolve_connect_command(
    cli_connect_command: str | None,
    mngr_ctx: MngrContext,
) -> str | None:
    """Resolve the connect command from a CLI option or global config.

    Returns None (i.e. use the builtin connect) when already running inside a
    custom connect command, so a connect command that itself invokes mngr does
    not recurse indefinitely.
    """
    if os.environ.get(CONNECT_COMMAND_ACTIVE_ENV_VAR):
        return None
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
    # Guard against infinite recursion if the connect command invokes mngr again.
    env[CONNECT_COMMAND_ACTIVE_ENV_VAR] = "1"
    logger.debug("Running custom connect command: {}", connect_command)
    os.execvpe("sh", ["sh", "-c", connect_command], env)


def restore_terminal_modes(fd: int, attach_args: tuple[str, ...]) -> None:
    """Write TERMINAL_MODE_RESET to ``fd`` if it is a terminal an SSH session could have left modes on.

    In tmux control mode (``-C``/``-CC``) the terminal shows tmux's protocol rather than the
    pane, so nothing from the session reached its modes.
    """
    if os.isatty(fd) and _TMUX_CONTROL_MODE_FLAGS.isdisjoint(attach_args):
        os.write(fd, TERMINAL_MODE_RESET)


def _run_ssh_interactively(ssh_args: Sequence[str], env: Mapping[str, str]) -> int:
    return run_interactive_subprocess(ssh_args, env=env).returncode


def _build_remote_attach_args(
    agent: AgentInterface,
    host: OnlineHostInterface,
    mngr_ctx: MngrContext,
    connection_opts: ConnectionOptions,
) -> list[str]:
    """Build the ssh argv that attaches to the agent's tmux session on ``host``."""
    ssh_args = _build_ssh_args(host, connection_opts)
    wrapper_script = _build_ssh_activity_wrapper_script(
        agent.session_name,
        agent.id,
        host.host_dir,
        mngr_ctx.config.tmux.attach_args,
    )
    # Pass the wrapper as a single remote command string so SSH doesn't
    # split it into separate words. SSH concatenates multiple remote command
    # arguments with spaces, which would cause 'bash -c' to only receive
    # the first word of the script (e.g., 'mkdir') instead of the full script.
    ssh_args.extend(["-t", "bash -c " + shlex.quote(wrapper_script)])
    return ssh_args


def _resolve_host_after_lost_connection(
    agent: AgentInterface,
    host: OnlineHostInterface,
    provider: BaseProviderInstance,
    is_auto_start_allowed: bool,
) -> tuple[AgentInterface, OnlineHostInterface, bool]:
    """Look the host up again after ssh lost its connection, restoring it if it paused meanwhile.

    Returns the agent and host to reconnect to and whether the host was restored. A restored
    host is a new machine at a new address, so the caller must rebuild its ssh argv. Any state
    other than online or PAUSED raises: a deliberately stopped, destroyed or failed host stays
    down, and a CRASHED one is not restored because a host whose provider listing lags its new
    sandbox reads the same way.
    """
    provider.on_connection_error(host.id)
    provider.reset_caches()
    current_host = provider.get_host(host.id)
    if isinstance(current_host, OnlineHostInterface):
        return agent, current_host, False
    state = current_host.get_state()
    if state != HostState.PAUSED:
        raise HostOfflineError(f"Lost the connection to agent {agent.name}: its host {host.id} is {state}")
    logger.info("Host {} paused while disconnected", host.id)
    restored_host, _ = ensure_host_started(current_host, is_start_desired=is_auto_start_allowed, provider=provider)
    for restored_agent in restored_host.get_agents():
        if restored_agent.id == agent.id:
            ensure_agent_started(restored_agent, restored_host, is_start_desired=is_auto_start_allowed)
            return restored_agent, restored_host, True
    raise AgentNotFoundOnHostError(agent.id, host.id)


def _attach_once(
    ssh_args: Sequence[str],
    env: Mapping[str, str],
    attach_args: tuple[str, ...],
    run_ssh: Callable[[Sequence[str], Mapping[str, str]], int],
    read_clock: Callable[[], ClockReading],
    terminal_fd: int,
) -> tuple[int, bool]:
    """Run one ssh session, then reset the terminal it ran on if it lost its connection.

    Returns ssh's exit code and whether this machine was suspended while it ran.
    """
    started_at = read_clock()
    exit_code = run_ssh(ssh_args, env)
    if exit_code == SSH_ERROR_EXIT_CODE:
        restore_terminal_modes(terminal_fd, attach_args)
    return exit_code, was_suspended_since(started_at, now=read_clock())


def _run_remote_session(
    agent: AgentInterface,
    host: OnlineHostInterface,
    mngr_ctx: MngrContext,
    connection_opts: ConnectionOptions,
    provider: BaseProviderInstance,
    run_ssh: Callable[[Sequence[str], Mapping[str, str]], int],
    exec_action: Callable[[str, list[str]], None],
    read_clock: Callable[[], ClockReading],
    terminal_fd: int,
) -> None:
    """Attach to a remote agent over ssh, reconnecting while ssh reports a lost connection.

    A drop that spans a suspension of this machine, or that is followed by restoring a host that
    paused, reconnects straight away without spending a retry; other drops spend one retry each.
    """
    # hack to make this work for me, we really should be more principled about this...
    env = {**os.environ}
    if env.get("TERM") == "xterm-kitty":
        env["TERM"] = "xterm-256color"

    attach_args = mngr_ctx.config.tmux.attach_args
    retry_delay_seconds = parse_duration_to_seconds(connection_opts.retry_delay)
    retries_left = connection_opts.retry_count
    exit_code, was_suspended = _attach_once(
        _build_remote_attach_args(agent, host, mngr_ctx, connection_opts),
        env,
        attach_args,
        run_ssh,
        read_clock,
        terminal_fd,
    )
    while exit_code == SSH_ERROR_EXIT_CODE:
        agent, host, was_restored = _resolve_host_after_lost_connection(
            agent, host, provider, connection_opts.is_auto_start_allowed
        )
        if was_suspended or was_restored:
            retries_left = connection_opts.retry_count
            logger.info("Reconnecting to agent {}...", agent.name)
        elif retries_left <= 0:
            raise HostConnectionError(
                f"Could not connect to agent {agent.name} over SSH after {connection_opts.retry_count} retries"
            )
        else:
            retries_left -= 1
            logger.warning(
                "SSH connection failed (exit code {}). Retrying in {} ({}/{})...",
                exit_code,
                connection_opts.retry_delay,
                connection_opts.retry_count - retries_left,
                connection_opts.retry_count,
            )
            poll_until(lambda: False, timeout=retry_delay_seconds, poll_interval=retry_delay_seconds)
        exit_code, was_suspended = _attach_once(
            _build_remote_attach_args(agent, host, mngr_ctx, connection_opts),
            env,
            attach_args,
            run_ssh,
            read_clock,
            terminal_fd,
        )

    action = _determine_post_disconnect_action(exit_code, agent.id, host.id)
    if action is not None:
        executable, argv = action
        logger.info("Running post-disconnect action: {}", argv)
        exec_action(executable, argv)
    elif exit_code != 0:
        raise MngrError(f"The SSH session to agent {agent.name} ended with exit code {exit_code}")
    else:
        logger.debug("SSH session ended normally")


def connect_to_agent(
    agent: AgentInterface,
    host: OnlineHostInterface,
    mngr_ctx: MngrContext,
    connection_opts: ConnectionOptions,
    *,
    # Keyword arguments so tests can substitute in-memory fakes.
    provider: BaseProviderInstance | None = None,
    run_ssh: Callable[[Sequence[str], Mapping[str, str]], int] = _run_ssh_interactively,
    exec_action: Callable[[str, list[str]], None] = os.execvp,
    read_clock: Callable[[], ClockReading] = read_clocks,
    terminal_fd: int = _STDOUT_FD,
) -> None:
    """Connect to an agent via tmux attach (local) or SSH + tmux attach (remote).

    For local agents, replaces the current process with: tmux attach -t =<session_name>

    For remote agents, runs SSH interactively and then checks the exit code to
    determine if a post-disconnect action (destroy/stop) was requested via the
    tmux key bindings (Ctrl-q for destroy, Ctrl-t for stop). If so, replaces the
    current process with the appropriate mngr command to perform the action locally.
    When ssh loses its connection, the terminal modes the session may have left on are
    switched off and the session is reattached, restoring the host first if it paused.

    For local agents, this function does not return (os.execvp replaces the process).
    For remote agents, this function returns after the SSH session ends unless a
    post-disconnect action is triggered (in which case os.execvp replaces the process),
    and raises if the session cannot be reattached or ends with an unexpected exit status.
    """
    logger.info("Connecting to agent...")

    session_name = agent.session_name
    target = TmuxSessionTarget(session_name=session_name)

    if host.is_local:
        # Detect nested tmux: if $TMUX is set, we're inside a tmux session
        env = os.environ
        if os.environ.get("TMUX"):
            if not mngr_ctx.config.is_nested_tmux_allowed:
                raise NestedTmuxError(session_name)
            # Copy and remove TMUX so tmux allows the nested attachment
            env = dict(os.environ)
            del env["TMUX"]
        # build_attach_argv uses target.as_target_arg() (the raw `=name`), not
        # as_shell_arg(): argv reaches os.execvpe verbatim, so shell-quoting it
        # would wrong-shape the argument whenever the name has a shell-special
        # character. The `=` still forces exact-session matching either way.
        os.execvpe("tmux", build_attach_argv(target, mngr_ctx.config.tmux.attach_args), env)
    else:
        _run_remote_session(
            agent,
            host,
            mngr_ctx,
            connection_opts,
            provider=provider if provider is not None else get_provider_instance(host.get_provider_name(), mngr_ctx),
            run_ssh=run_ssh,
            exec_action=exec_action,
            read_clock=read_clock,
            terminal_fd=terminal_fd,
        )
