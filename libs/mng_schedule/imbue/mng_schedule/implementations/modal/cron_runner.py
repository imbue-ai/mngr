# Modal app for running a scheduled mng command on a cron schedule.
#
# This file is deployed via `modal deploy` and runs as a cron-scheduled Modal
# Function. The module-level code handles deploy-time configuration (reading
# env vars, building the image). The runtime function runs the configured mng
# command.
#
# IMPORTANT: This file must NOT import from imbue.* or any other 3rd-party packages
# We simply want to call into the mng command, which can then use those other packages if necessary.
# This avoids modal needing to package or load any additional dependencies.
#
# Image building strategy:
# 1. Base image: built from the mng Dockerfile, which provides a complete
#    environment with system deps, Python, uv, Claude Code, and mng installed.
#    For EDITABLE mode, the mng monorepo tarball is in the build context.
#    For PACKAGE mode, a modified Dockerfile installs mng from PyPI instead.
# 2. Target repo layer: the user's project tarball is extracted to the
#    configured target_repo_path (default /code/project).
# 3. Staging layer: deploy files (config, secrets, settings) are baked into
#    their final locations ($HOME and WORKDIR).
#
# Required environment variables at deploy time:
# - SCHEDULE_DEPLOY_CONFIG: JSON string with all deploy configuration
# - SCHEDULE_BUILD_CONTEXT_DIR: Local path to mng build context (monorepo tarball for editable, empty for package)
# - SCHEDULE_STAGING_DIR: Local path to staging directory (deploy files + secrets)
# - SCHEDULE_DOCKERFILE: Local path to mng Dockerfile (or modified version for package mode)
# - SCHEDULE_TARGET_REPO_DIR: Local path to directory containing the target repo tarball
import datetime
import json
import os
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
# 1. Base: mng Dockerfile (system deps, uv, Claude Code, mng installed)
# 2. Target repo: user's project tarball extracted to target_repo_path
# 3. Staging: deploy files (config, secrets) baked into $HOME and WORKDIR

if modal.is_local():
    # 1. Build base image from the mng Dockerfile
    _image = modal.Image.from_dockerfile(
        _DOCKERFILE,
        context_dir=_BUILD_CONTEXT_DIR,
    )

    # this is only skipped if the target repo and mng repo are the same, eg, is an optimization for faster builds when iterating on mng itself
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
            "RUN cp -a /staging/home/. $HOME/",
            "RUN cp -a /staging/project/. .",
        ]
    )
else:
    # At runtime, the image is already built
    _image = modal.Image.debian_slim()

app = modal.App(name=_APP_NAME, image=_image)


# --- Runtime functions ---


def _run_and_stream(
    cmd: list[str] | str,
    *,
    is_checked: bool = True,
    cwd: str | None = None,
    is_shell: bool = False,
) -> int:
    """Run a command, streaming output to stdout in real time."""
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
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
    process.wait()
    if is_checked and process.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {process.returncode}: {cmd}")
    return process.returncode


@app.function(
    schedule=modal.Cron(_CRON_SCHEDULE, timezone=_CRON_TIMEZONE),
    timeout=3600,
)
def run_scheduled_trigger() -> None:
    """Run the scheduled mng command.

    This function executes on the cron schedule and:
    1. Checks if the trigger is enabled
    2. Loads consolidated environment variables from the secrets env file
    3. Sets up GitHub authentication
    4. Builds and runs the mng command with secrets env file

    Deploy files (config, settings, etc.) are already baked into $HOME and
    WORKDIR during the image build via dockerfile_commands.
    """
    trigger = _deploy_config["trigger"]

    if not trigger.get("is_enabled", True):
        print("Schedule trigger is disabled, skipping")
        return

    # Load consolidated env vars into the process environment so that the
    # mng CLI and any subprocesses it spawns have access to them.
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

    # Build the mng command (command is stored uppercase from the enum, mng CLI expects lowercase)
    command = trigger["command"].lower()
    args_str = trigger.get("args", "")

    # format the initial message
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
    formatted_args_str = args_str.format(DATE=now_str)

    cmd = ["mng", command]
    if formatted_args_str:
        cmd.extend(shlex.split(formatted_args_str))

    # Also pass the secrets env file via --host-env-file for create/start commands
    # so the agent host inherits these environment variables.
    secrets_env = Path("/staging/secrets/.env")
    if secrets_env.exists() and command in ("create", "start"):
        cmd.extend(["--host-env-file", str(secrets_env)])

    print(f"Currently in {os.getcwd()}")

    print(f"Running: {' '.join(cmd)}")
    exit_code = _run_and_stream(cmd, is_checked=False)
    if exit_code != 0:
        raise RuntimeError(f"mng {command} failed with exit code {exit_code}")
