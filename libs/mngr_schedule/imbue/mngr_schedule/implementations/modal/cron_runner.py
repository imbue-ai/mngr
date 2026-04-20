# Modal app for running a scheduled mngr command on a cron schedule.
#
# This file is deployed via `modal deploy` and runs as a cron-scheduled Modal
# Function. The module-level code handles deploy-time configuration (reading
# env vars, building the image). The runtime function runs the configured mngr
# command.
#
# IMPORTANT: This file must NOT import from imbue.* or any other 3rd-party packages
# We simply want to call into the mngr command, which can then use those other packages if necessary.
# This avoids modal needing to package or load any additional dependencies.
#
# Image building strategy:
# 1. Base image: built from the mngr Dockerfile, which provides a complete
#    environment with system deps, Python, uv, Claude Code, and mngr installed.
#    For EDITABLE mode, the mngr monorepo tarball is in the build context.
#    For PACKAGE mode, a modified Dockerfile installs mngr from PyPI instead.
# 2. Target repo layer: the user's project tarball is extracted to the
#    configured target_repo_path (default /code/project).
# 3. Staging layer: deploy files (config, secrets, settings) are baked into
#    their final locations ($HOME and WORKDIR).
#
# Required environment variables at deploy time:
# - SCHEDULE_DEPLOY_CONFIG: JSON string with all deploy configuration
# - SCHEDULE_BUILD_CONTEXT_DIR: Local path to mngr build context (monorepo tarball for editable, empty for package)
# - SCHEDULE_STAGING_DIR: Local path to staging directory (deploy files + secrets)
# - SCHEDULE_DOCKERFILE: Local path to mngr Dockerfile (or modified version for package mode)
# - SCHEDULE_TARGET_REPO_DIR: Local path to directory containing the target repo tarball
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import modal

# --- Deploy-time configuration ---
# At deploy time (modal.is_local() == True), we read configuration from a
# single JSON env var and write it to /staging/deploy_config.json. At runtime,
# we read from that baked-in file. Local filesystem paths (build context,
# staging dir, dockerfile) are separate env vars since they're only needed
# at deploy time for image building.


def _require_env(name: str) -> str:
    """Read a required environment variable, raising if missing."""
    value = os.environ.get(name)
    if value is None:
        raise RuntimeError(f"{name} must be set")
    return value


if modal.is_local():
    _deploy_config_json: str = _require_env("SCHEDULE_DEPLOY_CONFIG")
    _deploy_config: dict[str, Any] = json.loads(_deploy_config_json)

    # Local filesystem paths only needed at deploy time for image building
    _BUILD_CONTEXT_DIR: str = _require_env("SCHEDULE_BUILD_CONTEXT_DIR")
    _STAGING_DIR: str = _require_env("SCHEDULE_STAGING_DIR")
    _DOCKERFILE: str = _require_env("SCHEDULE_DOCKERFILE")
    _TARGET_REPO_DIR: str | None = os.environ.get("SCHEDULE_TARGET_REPO_DIR", None)
else:
    _deploy_config: dict[str, Any] = json.loads(Path("/staging/deploy_config.json").read_text())

    _BUILD_CONTEXT_DIR = ""
    _STAGING_DIR = ""
    _DOCKERFILE = ""
    _TARGET_REPO_DIR = ""

# Extract config values used by both deploy-time image building and runtime scheduling
_APP_NAME: str = _deploy_config["app_name"]
_CRON_SCHEDULE: str = _deploy_config["cron_schedule"]
_CRON_TIMEZONE: str = _deploy_config["cron_timezone"]
_TARGET_REPO_PATH: str = _deploy_config.get("target_repo_path", "/code/project")
# Branch to fetch/merge at runtime, or None to skip auto-merge entirely
_AUTO_MERGE_BRANCH: str | None = _deploy_config.get("auto_merge_branch")


# --- Image definition ---
# The image is built in layers:
# 1. Base: mngr Dockerfile (system deps, uv, Claude Code, mngr installed)
# 2. Target repo: user's project tarball extracted to target_repo_path
# 3. Staging: deploy files (config, secrets) baked into $HOME and WORKDIR

if modal.is_local():
    # 1. Build base image from the mngr Dockerfile
    _image = modal.Image.from_dockerfile(
        _DOCKERFILE,
        context_dir=_BUILD_CONTEXT_DIR,
    )

    # this is only skipped if the target repo and mngr repo are the same, eg, is an optimization for faster builds when iterating on mngr itself
    if _TARGET_REPO_DIR is not None:
        # 2. Add the target repo tarball and extract it to the configured path
        _image = _image.add_local_dir(
            _TARGET_REPO_DIR,
            "/target_repo",
            copy=True,
        ).dockerfile_commands(
            [
                f"RUN mkdir -p {_TARGET_REPO_PATH} && tar -xzf /target_repo/current.tar.gz -C {_TARGET_REPO_PATH} && rm -rf /target_repo",
                f"RUN git config --global --add safe.directory {_TARGET_REPO_PATH}",
                f"RUN git config --global --add safe.directory {_TARGET_REPO_PATH}/.git",
                f"WORKDIR {_TARGET_REPO_PATH}",
            ]
        )

    # 3. Add staging files and bake them into their final locations
    _image = _image.add_local_dir(
        _STAGING_DIR,
        "/staging",
        copy=True,
    ).dockerfile_commands(
        [
            # Guard with -d because Modal's add_local_dir skips empty directories,
            # so /staging/project/ won't exist when no plugins stage project files.
            # Use `if`/`then`/`fi` (not `&& cp || true`) so that a genuine cp
            # failure (e.g. permission denied) still fails the build rather than
            # being swallowed alongside the missing-directory case.
            'RUN if [ -d /staging/home ]; then cp -a /staging/home/. "$HOME"/; fi',
            "RUN if [ -d /staging/project ]; then cp -a /staging/project/. .; fi",
        ]
    )
else:
    # At runtime, the image is already built
    _image = modal.Image.debian_slim()

app = modal.App(name=_APP_NAME, image=_image)


# --- Runtime functions ---

# Sentinel line prefix used to communicate a structured verification result to
# the deploying machine. The line is written once, on its own, at the end of
# the runtime function. The deploy-side verification parser looks for this
# exact prefix.
_RESULT_SENTINEL: str = "__MNGR_SCHEDULE_VERIFY__"

# Regex that extracts the agent name from `mngr create` output. The CLI logs
# a line like: "Starting agent <name> ..." once the agent has been created.
_AGENT_NAME_PATTERN: re.Pattern[str] = re.compile(r"Starting agent\s+(\S+)")

# Lifecycle states (as reported by `mngr list --format json`) that indicate
# the agent is still actively running. Any other state is treated as terminal.
_RUNNING_STATES: frozenset[str] = frozenset({"RUNNING", "WAITING", "REPLACED", "RUNNING_UNKNOWN_AGENT_TYPE"})

# Accepted values for the `verify_mode` parameter of `run_scheduled_trigger`.
# Kept as bare strings (not the VerifyMode enum) because cron_runner.py is
# forbidden from importing from imbue.*; see the file-level comment.
_VALID_VERIFY_MODES: frozenset[str] = frozenset({"none", "quick", "full"})

# Private sentinel returned by `_get_lifecycle_state` when the named agent is
# not present in `mngr list` output. Deliberately distinct from any real
# AgentLifecycleState value so the deploy-side verifier can tell "agent
# vanished" apart from "agent reached an unexpected terminal state". The
# value must be kept in sync with the matching constant in verification.py.
_AGENT_MISSING_STATE: str = "MISSING"

# How often to poll the agent's lifecycle state during full verification.
_AGENT_POLL_INTERVAL_SECONDS: float = 10.0

# Maximum time to wait for the agent to reach a terminal state during full
# verification. Kept below the Modal function timeout so the function can
# return the failure result instead of being killed.
_AGENT_FINISH_TIMEOUT_SECONDS: float = 3400.0


def _run_and_stream(
    cmd: list[str] | str,
    *,
    is_checked: bool = True,
    cwd: str | None = None,
    is_shell: bool = False,
) -> tuple[int, str]:
    """Run a command, streaming output to stdout in real time.

    Returns (exit_code, captured_output). The captured output contains
    the full stdout+stderr of the command. On failure, the last 50 lines
    are included in the RuntimeError for diagnostics.
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=cwd,
        shell=is_shell,
    )
    assert process.stdout is not None
    captured_lines: list[str] = []
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        captured_lines.append(line)
    process.wait()
    full_output = "".join(captured_lines)
    if is_checked and process.returncode != 0:
        tail = "".join(captured_lines[-50:])
        raise RuntimeError(f"Command failed with exit code {process.returncode}: {cmd}\nLast output:\n{tail}")
    return process.returncode, full_output


def _get_lifecycle_state(agent_name: str) -> str | None:
    """Look up the lifecycle state of the named agent.

    Shells `mngr list --provider local --include 'name == "<agent_name>"'
    --format json` and parses the result. A transient subprocess failure
    yields None so the caller can retry; an absent agent yields
    `_AGENT_MISSING_STATE`; otherwise yields the state string reported by
    mngr.
    """
    # Agent names from `Starting agent <name>` are produced by mngr itself and
    # use a restricted character set ([\w-]+), so interpolating into the CEL
    # expression is safe. Guard against unexpected characters defensively.
    if not re.fullmatch(r"[\w-]+", agent_name):
        raise RuntimeError(f"unexpected agent name for lifecycle lookup: {agent_name!r}")
    try:
        completed = subprocess.run(
            [
                "mngr",
                "list",
                "--provider",
                "local",
                "--include",
                f'name == "{agent_name}"',
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        # Surface the failure so a persistently-hanging `mngr list` is
        # visible in the container logs rather than only manifesting as a
        # generic "timed out waiting for agent" at the end of the poll loop.
        print(f"mngr list for agent {agent_name!r} timed out after {exc.timeout}s")
        return None
    if completed.returncode != 0:
        # Truncate stderr so a runaway error doesn't flood the logs every
        # poll interval; the tail is what matters for diagnostics.
        stderr_tail = completed.stderr[-2000:] if completed.stderr else ""
        print(
            f"mngr list for agent {agent_name!r} exited with code {completed.returncode}; stderr tail:\n{stderr_tail}"
        )
        return None
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        stdout_preview = completed.stdout[:2000] if completed.stdout else ""
        print(
            f"mngr list for agent {agent_name!r} returned non-JSON output ({exc}); stdout preview:\n{stdout_preview}"
        )
        return None
    agents = data.get("agents", [])
    if not agents:
        return _AGENT_MISSING_STATE
    state = agents[0].get("state")
    return str(state) if state is not None else None


def _poll_until_done(
    agent_name: str,
    timeout_seconds: float = _AGENT_FINISH_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _AGENT_POLL_INTERVAL_SECONDS,
) -> str:
    """Poll the agent's lifecycle state until it leaves the running states.

    Returns the final state string. Raises RuntimeError on timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        state = _get_lifecycle_state(agent_name)
        if state is not None and state not in _RUNNING_STATES:
            return state
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for agent '{agent_name}' to finish after {timeout_seconds:.0f}s")
        time.sleep(poll_interval_seconds)


def _destroy_agent(agent_name: str) -> tuple[int, str]:
    """Destroy the named agent via the mngr CLI (best-effort; --force swallows not-found).

    Returns (exit_code, stderr). The caller is responsible for surfacing the
    result (typically by including it in the verification result dict).

    If the subprocess times out, returns a sentinel exit code of -1 with a
    stderr message explaining the timeout, so the caller can emit the
    structured result instead of letting TimeoutExpired propagate and
    break the sentinel contract.
    """
    try:
        completed = subprocess.run(
            ["mngr", "destroy", "--force", agent_name],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        return -1, f"`mngr destroy --force {agent_name}` timed out after {exc.timeout}s"
    return completed.returncode, completed.stderr


def _print_result_sentinel(result: dict[str, Any]) -> None:
    """Write the structured verification result on a single line for the deploy-side parser."""
    sys.stdout.write(f"{_RESULT_SENTINEL} {json.dumps(result)}\n")
    sys.stdout.flush()


@app.function(
    schedule=modal.Cron(_CRON_SCHEDULE, timezone=_CRON_TIMEZONE),
    timeout=3600,
)
def run_scheduled_trigger(verify_mode: str = "none") -> dict[str, Any]:
    """Run the scheduled mngr command and return a structured result.

    Scheduled (cron-driven) invocations call this with no arguments, so
    `verify_mode` defaults to "none" and no post-create verification runs.
    The deploy-side verifier invokes the function manually via `modal run
    ... --verify-mode quick|full` to exercise the post-create path from
    inside the same container that owns the agent.

    Steps:
    1. Check if the trigger is enabled
    2. Load consolidated environment variables from the secrets env file
    3. Optionally set up GitHub authentication and auto-merge
    4. Build and run the mngr command with secrets env file
    5. If the command is `create` and verify_mode is not "none", extract the
       agent name from the output and either destroy it (quick) or poll its
       lifecycle state until it finishes (full)
    6. Emit a sentinel line with the structured result and return the same
       dict, so callers using either `modal run` or `fn.remote()` can inspect
       the outcome

    Raises RuntimeError if the underlying mngr command fails or the verify
    step times out.
    """
    # Validate verify_mode up front, before any side effects. Otherwise an
    # invalid value would only be caught after `mngr create` has already
    # created an agent, leaving it orphaned because we would raise before
    # the destroy/poll path could run.
    normalized_verify = verify_mode.lower()
    if normalized_verify not in _VALID_VERIFY_MODES:
        raise RuntimeError(f"unknown verify_mode: {verify_mode!r} (expected one of {sorted(_VALID_VERIFY_MODES)})")

    trigger = _deploy_config["trigger"]

    if not trigger.get("is_enabled", True):
        print("Schedule trigger is disabled, skipping")
        result: dict[str, Any] = {"status": "disabled"}
        _print_result_sentinel(result)
        return result

    # Load consolidated env vars into the process environment so that the
    # mngr CLI and any subprocesses it spawns have access to them.
    secrets_json_path = Path("/staging/secrets/env.json")
    if secrets_json_path.exists():
        print("Loading environment variables from secrets env file...")
        for key, value in json.loads(secrets_json_path.read_text()).items():
            if value is not None:
                print(f"Setting env var: {key}")
                os.environ[key] = value

    # If auto-merge is enabled, set up GitHub authentication and fetch/merge the
    # latest code from the configured branch before running the command.
    if _AUTO_MERGE_BRANCH is not None:
        print("Setting up GitHub authentication...")
        os.makedirs(os.path.expanduser("~/.ssh"), mode=0o700, exist_ok=True)
        _run_and_stream(
            "ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null && gh auth setup-git",
            is_shell=True,
        )

        print(f"Auto-merging latest code from branch '{_AUTO_MERGE_BRANCH}'...")
        _run_and_stream(["git", "fetch", "origin", _AUTO_MERGE_BRANCH])
        _run_and_stream(["git", "checkout", _AUTO_MERGE_BRANCH])
        _run_and_stream(["git", "merge", f"origin/{_AUTO_MERGE_BRANCH}"])

    # Build the mngr command (command is stored uppercase from the enum, mngr CLI expects lowercase)
    command = trigger["command"].lower()
    args_str = trigger.get("args", "")

    # format the initial message
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
    formatted_args_str = args_str.format(DATE=now_str)

    cmd = ["mngr", command]
    if formatted_args_str:
        cmd.extend(shlex.split(formatted_args_str))

    # Also pass the secrets env file via --host-env-file for create/start commands
    # so the agent host inherits these environment variables.
    secrets_env = Path("/staging/secrets/.env")
    if secrets_env.exists() and command in ("create", "start"):
        cmd.extend(["--host-env-file", str(secrets_env)])

    print(f"Currently in {os.getcwd()}")

    print(f"Running: {' '.join(cmd)}")
    exit_code, full_output = _run_and_stream(cmd, is_checked=False)
    if exit_code != 0:
        raise RuntimeError(f"mngr {command} failed with exit code {exit_code}\nOutput:\n{full_output}")

    # Include `output` so callers using fn.remote() (e.g. `mngr schedule run
    # --provider modal`) can print the captured command output. fn.remote()
    # does not stream container stdout to the caller, so this field is the
    # only way that output reaches the local process.
    result: dict[str, Any] = {
        "status": "ok",
        "command": command,
        "verify_mode": normalized_verify,
        "output": full_output,
    }

    if command != "create" or normalized_verify == "none":
        _print_result_sentinel(result)
        return result

    match = _AGENT_NAME_PATTERN.search(full_output)
    if match is None:
        # No agent name to act on -- still a successful mngr create from the
        # CLI's perspective, but we couldn't do in-container verify. Surface
        # this so the deploy side can decide whether to fail.
        result["verify"] = {"status": "no_agent_name"}
        _print_result_sentinel(result)
        return result

    agent_name = match.group(1)
    result["agent_name"] = agent_name

    if normalized_verify == "quick":
        destroy_exit_code, destroy_stderr = _destroy_agent(agent_name)
        result["verify"] = {
            "status": "destroyed",
            "destroy_exit_code": destroy_exit_code,
            "destroy_stderr": destroy_stderr,
        }
    else:
        try:
            final_state = _poll_until_done(agent_name)
        except RuntimeError as exc:
            # Full-verify hit its inner timeout. Best-effort destroy so the
            # agent is not orphaned, then report the timeout via the sentinel
            # (instead of propagating the RuntimeError, which would bypass
            # _print_result_sentinel and leave the deploy side with only a
            # generic non-zero-exit error).
            destroy_exit_code, destroy_stderr = _destroy_agent(agent_name)
            result["verify"] = {
                "status": "timeout",
                "timeout_message": str(exc),
                "destroy_exit_code": destroy_exit_code,
                "destroy_stderr": destroy_stderr,
            }
        else:
            result["verify"] = {"status": "finished", "final_state": final_state}

    _print_result_sentinel(result)
    return result
