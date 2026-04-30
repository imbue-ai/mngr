# Modal app for running a scheduled mngr command on a cron schedule.
#
# This file is deployed via `modal deploy` and runs as a cron-scheduled Modal
# Function. The module-level code handles deploy-time configuration (reading
# env vars, building the image). The runtime function runs the configured mngr
# command.
#
# IMPORTANT: This file must NOT import from imbue.* or any other 3rd-party packages
# at module scope. Modal runs this file as a standalone app — its Python
# interpreter does NOT inherit the uv-tool-managed mngr install's site-packages,
# so `from imbue.mngr_schedule... import X` raises ModuleNotFoundError at deploy
# time. Values that mirror imbue enums (RUNNING_STATES, VALID_VERIFY_MODES)
# and the RESULT_SENTINEL must therefore be duplicated here as
# bare literals; verification.py defines the deploy-side copies. Any changes
# must be mirrored by hand in both files.
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

    # Deploy-time-only paths: never consumed at runtime (all usages are
    # guarded by ``if modal.is_local()``), but keep the annotations aligned
    # with the local branch so the module-level types are unambiguous.
    _BUILD_CONTEXT_DIR: str = ""
    _STAGING_DIR: str = ""
    _DOCKERFILE: str = ""
    _TARGET_REPO_DIR: str | None = None

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


class CronRunnerError(Exception):
    """Errors raised by the in-container verify path.

    cron_runner.py is forbidden from importing imbue exception types (see
    the file-level comment). Raising a locally-defined exception class
    instead of a built-in gives the verify path a namespaced error type
    without violating the import policy.
    """


# Lifecycle states (as reported by `mngr list --format json`) that indicate
# the agent is still actively running. Mirror of the running subset of
# `imbue.mngr.primitives.AgentLifecycleState`; verification.py defines the
# deploy-side copy. Any change must be applied in both places.
RUNNING_STATES: frozenset[str] = frozenset({"RUNNING", "WAITING", "REPLACED", "RUNNING_UNKNOWN_AGENT_TYPE"})

# Accepted values for the `verify_mode` parameter of `run_scheduled_trigger`.
# Mirror of `imbue.mngr_schedule.data_types.VerifyMode` values (lowercased);
# duplicated here because Modal runs this file outside the imbue namespace.
VALID_VERIFY_MODES: frozenset[str] = frozenset({"none", "quick", "full"})

# Sentinel line prefix used to communicate a structured verification result
# to the deploying machine. Must match verification._RESULT_SENTINEL exactly.
RESULT_SENTINEL: str = "__MNGR_SCHEDULE_VERIFY__"

# Regex that extracts the agent name from `mngr create` output. The CLI logs
# a line like: "Starting agent <name> ..." once the agent has been created.
_AGENT_NAME_PATTERN: re.Pattern[str] = re.compile(r"Starting agent\s+(\S+)")

# Maximum time to wait for the agent to reach a terminal state during full
# verification. Kept far enough below the Modal function timeout (3600s) that
# the full timeout path -- mngr wait exits with code 2, best-effort destroy
# (its own 300s subprocess timeout), sentinel emission -- still fits, even
# after `mngr create` has already consumed some of the function's budget.
# Rough accounting: 3600 - 180 (mngr create) - 300 (destroy) - 20 (overhead)
# = 3100s; using 3000s leaves additional slack so the container is never
# killed before `_print_result_sentinel` runs.
_AGENT_FINISH_TIMEOUT_SECONDS: float = 3000.0


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
    # Direct stdlib spawn rather than ConcurrencyGroup: cron_runner.py
    # is forbidden from importing imbue.* at module scope (Modal can't
    # see the namespace), so the in-container verify path uses the
    # stdlib directly. Same reason applies to _wait_for_agent_terminal_state
    # and _destroy_agent below.
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


def _wait_for_agent_terminal_state(
    agent_name: str,
    timeout_seconds: float = _AGENT_FINISH_TIMEOUT_SECONDS,
) -> str:
    """Block until the agent reaches a terminal lifecycle state, via `mngr wait`.

    Returns the final agent state. Raises CronRunnerError on timeout or
    if mngr wait fails (agent not found, non-zero exit, malformed JSON).
    `mngr wait` already handles transient state-lookup failures internally;
    we don't need our own poll loop on top.
    """
    if not re.fullmatch(r"[\w-]+", agent_name):
        raise CronRunnerError(f"unexpected agent name for wait: {agent_name!r}")
    try:
        completed = subprocess.run(
            [
                "mngr",
                "wait",
                agent_name,
                "--timeout",
                f"{int(timeout_seconds)}s",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            # +60s slack so mngr wait's own timeout fires (returncode=2)
            # before our subprocess.TimeoutExpired hides it.
            timeout=timeout_seconds + 60,
        )
    except subprocess.TimeoutExpired as exc:
        raise CronRunnerError(f"mngr wait for agent '{agent_name}' did not exit within {exc.timeout:.0f}s") from exc
    if completed.returncode == 2:
        raise CronRunnerError(f"mngr wait timed out waiting for agent '{agent_name}' after {timeout_seconds:.0f}s")
    if completed.returncode != 0:
        stderr_tail = completed.stderr[-2000:] if completed.stderr else ""
        raise CronRunnerError(
            f"mngr wait for agent '{agent_name}' exited with code {completed.returncode}; stderr tail:\n{stderr_tail}"
        )
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        stdout_preview = completed.stdout[:2000] if completed.stdout else ""
        raise CronRunnerError(
            f"mngr wait returned non-JSON output ({exc}); stdout preview:\n{stdout_preview}"
        ) from exc
    final_state = data.get("final_agent_state")
    if not isinstance(final_state, str):
        raise CronRunnerError(f"mngr wait result missing 'final_agent_state' string field; got: {data!r}")
    return final_state


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
    sys.stdout.write(f"{RESULT_SENTINEL} {json.dumps(result)}\n")
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

    Raises RuntimeError if the underlying mngr command fails, and
    CronRunnerError if verify_mode is invalid. A full-verify timeout is
    reported via the sentinel (status="timeout") rather than an exception.
    """
    # Validate verify_mode up front, before any side effects. Otherwise an
    # invalid value would only be caught after `mngr create` has already
    # created an agent, leaving it orphaned because we would raise before
    # the destroy/poll path could run.
    normalized_verify = verify_mode.lower()
    if normalized_verify not in VALID_VERIFY_MODES:
        raise CronRunnerError(f"unknown verify_mode: {verify_mode!r}")

    trigger = _deploy_config["trigger"]

    if not trigger.get("is_enabled", True):
        print("Schedule trigger is disabled, skipping")
        # Include an empty `output` field so callers that extract
        # result["output"] (e.g. invoke_modal_trigger_function, which powers
        # `mngr schedule run --provider modal`) see the same dict shape they
        # get for a successful run. `mngr schedule run` deliberately invokes
        # disabled triggers with a warning; without this field, that call
        # would fail the result-shape check and raise a misleading
        # "re-deploy" error.
        disabled_result: dict[str, Any] = {"status": "disabled", "output": ""}
        _print_result_sentinel(disabled_result)
        return disabled_result

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
            final_state = _wait_for_agent_terminal_state(agent_name)
        except CronRunnerError as exc:
            # Full-verify could not observe a terminal state (mngr wait
            # timed out, or returned an error). Best-effort destroy so
            # the agent is not orphaned, then report the failure via the
            # sentinel (instead of propagating the error, which would
            # bypass _print_result_sentinel and leave the deploy side
            # with only a generic non-zero-exit error).
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
