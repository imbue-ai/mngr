import hashlib
import importlib.metadata
import importlib.resources
import json
import os
import platform
import shlex
import shutil
import sys
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

import modal.exception
from dotenv import dotenv_values
from loguru import logger
from pydantic import ValidationError

import imbue.mng.resources as mng_resources
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.errors import UserInputError
from imbue.mng.primitives import LogLevel
from imbue.mng.providers.deploy_utils import MngInstallMode
from imbue.mng.providers.deploy_utils import collect_deploy_files
from imbue.mng.providers.deploy_utils import detect_mng_install_mode as _shared_detect_mng_install_mode
from imbue.mng.providers.deploy_utils import resolve_mng_install_mode as _shared_resolve_mng_install_mode
from imbue.mng.providers.modal.instance import ModalProviderInstance
from imbue.mng_schedule.data_types import ModalScheduleCreationRecord
from imbue.mng_schedule.data_types import ScheduleTriggerDefinition
from imbue.mng_schedule.data_types import VerifyMode
from imbue.mng_schedule.errors import ScheduleDeployError
from imbue.mng_schedule.git import ensure_current_branch_is_pushed
from imbue.mng_schedule.git import get_current_mng_git_hash
from imbue.mng_schedule.git import resolve_git_ref
from imbue.mng_schedule.implementations.modal.verification import verify_schedule_deployment

_FALLBACK_TIMEZONE: Final[str] = "UTC"

# Default target directory inside the container where the target repo is extracted
_DEFAULT_TARGET_REPO_PATH: Final[str] = "/code/project"

# Path prefix on the state volume for schedule records
_SCHEDULE_RECORDS_PREFIX: Final[str] = "/plugin/schedule"


def _forward_output(line: str, is_stdout: bool) -> None:
    if is_stdout:
        logger.log(LogLevel.BUILD.value, "{}", line.rstrip(), source="modal deploy")
    else:
        stream = sys.stdout if is_stdout else sys.stderr
        stream.write(line)
        stream.flush()


@pure
def get_modal_app_name(trigger_name: str) -> str:
    return f"mng-schedule-{trigger_name}"


@pure
def _resolve_timezone_from_paths(
    etc_timezone_path: Path,
    etc_localtime_path: Path,
) -> str:
    """Resolve the IANA timezone name from filesystem paths."""
    if etc_timezone_path.exists():
        name = etc_timezone_path.read_text().strip()
        if name:
            return name

    if etc_localtime_path.is_symlink():
        target = str(etc_localtime_path.resolve())
        if "zoneinfo/" in target:
            return target.split("zoneinfo/")[-1]

    return _FALLBACK_TIMEZONE


def detect_local_timezone() -> str:
    """Detect the user's local IANA timezone name (e.g. 'America/Los_Angeles')."""
    return _resolve_timezone_from_paths(
        etc_timezone_path=Path("/etc/timezone"),
        etc_localtime_path=Path("/etc/localtime"),
    )


def get_repo_root() -> Path:
    """Find the git repository root directory.

    Raises ScheduleDeployError if not inside a git repository.
    """
    repo_root = try_get_repo_root()
    if repo_root is None:
        raise ScheduleDeployError(
            "Could not find git repository root. Must be run from within a git repository."
        ) from None
    return repo_root


def try_get_repo_root() -> Path | None:
    """Try to find the git repository root directory.

    Returns the repo root Path if inside a git repo, or None if not.
    """
    with ConcurrencyGroup(name="git-toplevel") as cg:
        result = cg.run_process_to_completion(
            ["git", "rev-parse", "--show-toplevel"],
            is_checked_after=False,
        )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def _ensure_modal_environment(environment_name: str) -> None:
    """Ensure a Modal environment exists, creating it if necessary."""
    with ConcurrencyGroup(name="modal-env-create") as cg:
        result = cg.run_process_to_completion(
            ["uv", "run", "modal", "environment", "create", environment_name],
            is_checked_after=False,
        )
    # Exit code 0 = created. Non-zero with "same name" = already exists (OK).
    if result.returncode != 0 and "same name" not in result.stderr:
        raise ScheduleDeployError(
            f"Failed to create Modal environment '{environment_name}': {result.stderr.strip()}"
        ) from None


def package_repo_at_commit(commit_hash: str, dest_dir: Path, repo_root: Path) -> None:
    """Package the repo at a specific commit into a tarball using make_tar_of_repo.sh.

    The script creates <dest_dir>/current.tar.gz containing the repo at the specified commit.
    Raises ScheduleDeployError if packaging fails.
    """
    script_path = repo_root / "scripts" / "make_tar_of_repo.sh"
    if not script_path.exists():
        raise ScheduleDeployError(f"Packaging script not found at {script_path}") from None

    dest_dir.mkdir(parents=True, exist_ok=True)

    with ConcurrencyGroup(name="package-repo") as cg:
        result = cg.run_process_to_completion(
            ["bash", str(script_path), commit_hash, str(dest_dir)],
            is_checked_after=False,
            cwd=repo_root,
        )
    if result.returncode != 0:
        raise ScheduleDeployError(
            f"Failed to package repo at commit {commit_hash}: {(result.stdout + result.stderr).strip()}"
        ) from None


def package_directory_as_tarball(source_dir: Path, dest_dir: Path) -> None:
    """Package a directory into a tarball at dest_dir/current.tar.gz.

    Unlike package_repo_at_commit(), this does not use git and simply
    creates a tarball of the entire directory contents. Used for --full-copy
    mode where we want to capture the current working tree state without
    relying on git.

    Raises ScheduleDeployError if packaging fails.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    with ConcurrencyGroup(name="package-directory") as cg:
        result = cg.run_process_to_completion(
            ["tar", "-czf", str(dest_dir / "current.tar.gz"), "-C", str(source_dir), "."],
            is_checked_after=False,
        )
    if result.returncode != 0:
        raise ScheduleDeployError(
            f"Failed to package directory {source_dir}: {(result.stdout + result.stderr).strip()}"
        ) from None


def detect_mng_install_mode() -> MngInstallMode:
    """Detect how mng-schedule is currently installed.

    Delegates to the shared detect_mng_install_mode utility, but first
    verifies that mng-schedule is installed (raising ScheduleDeployError
    if not).
    """
    try:
        importlib.metadata.distribution("mng-schedule")
    except importlib.metadata.PackageNotFoundError:
        raise ScheduleDeployError("mng-schedule package is not installed. Cannot determine install mode.") from None

    return _shared_detect_mng_install_mode("mng-schedule")


def resolve_mng_install_mode(mode: MngInstallMode) -> MngInstallMode:
    """Resolve AUTO mode to a concrete install mode, or pass through others."""
    return _shared_resolve_mng_install_mode(mode, "mng-schedule")


def _get_mng_schedule_source_dir() -> Path:
    """Get the source directory for an editable install of mng-schedule.

    Returns the directory containing pyproject.toml for mng-schedule.
    Raises ScheduleDeployError if it cannot be determined.
    """
    # In editable mode, the source files are at their original location.
    # We can find the package root by walking up from the plugin module file.
    plugin_file = Path(__file__).resolve()
    # __file__ is at: .../libs/mng_schedule/imbue/mng_schedule/implementations/modal/deploy.py
    # We need: .../libs/mng_schedule/
    candidate = plugin_file.parent.parent.parent.parent.parent
    if (candidate / "pyproject.toml").exists():
        return candidate
    raise ScheduleDeployError(f"Could not find mng-schedule source directory (tried {candidate})")


def _get_mng_repo_root() -> Path:
    """Get the git repository root of the mng monorepo.

    When mng-schedule is installed in editable mode, this finds the git
    repository root by running git rev-parse from the mng-schedule source
    directory.

    Raises ScheduleDeployError if the source directory cannot be found or
    is not in a git repository.
    """
    mng_schedule_src = _get_mng_schedule_source_dir()
    with ConcurrencyGroup(name="git-mng-toplevel") as cg:
        result = cg.run_process_to_completion(
            ["git", "rev-parse", "--show-toplevel"],
            is_checked_after=False,
            cwd=mng_schedule_src,
        )
    if result.returncode != 0:
        raise ScheduleDeployError(
            f"Could not find git repository root for mng-schedule source at {mng_schedule_src}: "
            f"{result.stderr.strip()}"
        ) from None
    return Path(result.stdout.strip())


def get_mng_dockerfile_path(mode: MngInstallMode) -> Path:
    """Get the path to the mng Dockerfile based on the install mode.

    For EDITABLE mode, the Dockerfile is found by navigating from the mng-schedule
    source directory to the mng resources directory within the monorepo.
    For PACKAGE mode, the Dockerfile is loaded from the installed mng package
    via importlib.resources.
    """
    match mode:
        case MngInstallMode.EDITABLE | MngInstallMode.SKIP:
            mng_repo_root = _get_mng_repo_root()
            dockerfile_path = mng_repo_root / "libs" / "mng" / "imbue" / "mng" / "resources" / "Dockerfile"
            if not dockerfile_path.exists():
                raise ScheduleDeployError(
                    f"mng Dockerfile not found at {dockerfile_path}. "
                    "Expected the mng monorepo to contain libs/mng/imbue/mng/resources/Dockerfile."
                )
            return dockerfile_path
        case MngInstallMode.PACKAGE:
            resources_dir = importlib.resources.files(mng_resources)
            dockerfile_resource = resources_dir / "Dockerfile"
            dockerfile_path = Path(str(dockerfile_resource))
            if not dockerfile_path.exists():
                raise ScheduleDeployError(
                    "mng Dockerfile not found in installed package. The mng package may be missing its resources."
                )
            return dockerfile_path
        case MngInstallMode.AUTO:
            raise ScheduleDeployError("AUTO mode must be resolved before getting Dockerfile path.")
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _build_package_mode_dockerfile(mng_dockerfile_content: str) -> str:
    """Build a Dockerfile for PACKAGE mode from the mng Dockerfile.

    Replaces the monorepo-specific installation steps (COPY, extraction,
    uv sync, uv tool install) with a pip install from PyPI. All preceding
    layers (system deps, uv, Claude Code) are preserved.

    The mng Dockerfile has a section that copies and extracts the monorepo
    tarball, syncs dependencies, and installs mng as a tool. For PACKAGE
    mode, we replace that entire section with a simple pip install.
    """
    lines = mng_dockerfile_content.splitlines()
    result_lines: list[str] = []
    is_in_install_section = False
    install_replacement_added = False

    for line in lines:
        stripped = line.strip()

        # Detect start of the mng monorepo install section: the COPY instruction
        # that copies the build context (containing the monorepo tarball) into /code/
        if not is_in_install_section and stripped.startswith("COPY") and "/code" in stripped:
            is_in_install_section = True
            if not install_replacement_added:
                result_lines.append("")
                result_lines.append("# Install mng from PyPI (PACKAGE mode)")
                result_lines.append("RUN uv pip install --system mng mng-schedule")
                install_replacement_added = True
            continue

        # Skip lines until we pass the last monorepo-specific install command.
        # The sentinel is any RUN line containing "uv tool install" (which may
        # be combined with other commands on the same line via &&).
        if is_in_install_section:
            if stripped.startswith("RUN") and "uv tool install" in stripped:
                is_in_install_section = False
                continue
            # Also skip WORKDIR, RUN uv sync, and the tarball extraction lines
            continue

        result_lines.append(line)

    if is_in_install_section:
        raise ScheduleDeployError(
            "Failed to generate PACKAGE mode Dockerfile: could not find the end of the monorepo "
            "install section (expected a 'RUN uv tool install' line after 'COPY . /code/'). "
            "The mng Dockerfile structure may have changed."
        )

    return "\n".join(result_lines) + "\n"


def parse_upload_spec(spec: str) -> tuple[Path, str]:
    """Parse an upload spec in SOURCE:DEST format.

    Raises ValueError if the spec is malformed or the source does not exist.
    """
    if ":" not in spec:
        raise ValueError(f"Upload spec must be in SOURCE:DEST format, got: {spec}")
    source_str, dest = spec.split(":", 1)
    source_path = Path(source_str)
    if not source_path.exists():
        raise ValueError(f"Upload source does not exist: {source_str}")
    if dest.startswith("/"):
        raise ValueError(f"Upload destination must be relative or start with '~', got: {dest}")
    return source_path, dest


def _collect_deploy_files(
    mng_ctx: MngContext,
    repo_root: Path,
    include_user_settings: bool = True,
    include_project_settings: bool = True,
) -> dict[Path, Path | str]:
    """Collect all files for deployment by calling the get_files_for_deploy hook.

    Delegates to the shared collect_deploy_files utility in core mng.
    Catches MngError (from absolute path validation) and re-raises as
    ScheduleDeployError for backward compatibility.
    """
    try:
        return collect_deploy_files(
            mng_ctx=mng_ctx,
            repo_root=repo_root,
            include_user_settings=include_user_settings,
            include_project_settings=include_project_settings,
        )
    except MngError as e:
        raise ScheduleDeployError(str(e)) from e


def stage_deploy_files(
    staging_dir: Path,
    mng_ctx: MngContext,
    repo_root: Path,
    include_user_settings: bool = True,
    include_project_settings: bool = True,
    pass_env: Sequence[str] = (),
    env_files: Sequence[Path] = (),
    uploads: Sequence[tuple[Path, str]] = (),
) -> None:
    """Stage files for deployment into a directory for baking into the Modal image.

    Collects files from all plugins via the get_files_for_deploy hook and stages
    them into a directory structure that mirrors their destination layout:

    - Paths starting with "~" are user home files, placed under "home/" with
      the "~/" prefix stripped (e.g. "~/.claude.json" -> "home/.claude.json").
    - Relative paths (no "~" prefix) are project files, placed under "project/"
      (e.g. "config/settings.toml" -> "project/config/settings.toml").

    These are then baked into their final locations during the image build via
    dockerfile_commands (home/ -> $HOME, project/ -> WORKDIR).

    Also consolidates environment variables from multiple sources into a single
    secrets/.env file, and stages any user-specified uploads.

    Stages:
    - home/: Files destined for the user's home directory
    - project/: Files destined for the project working directory
    - secrets/.env: Consolidated environment variables from all sources
    """
    staging_dir.mkdir(parents=True, exist_ok=True)

    # Collect files from all plugins via the hook
    deploy_files = _collect_deploy_files(
        mng_ctx,
        repo_root,
        include_user_settings=include_user_settings,
        include_project_settings=include_project_settings,
    )

    # Create both staging subdirectories unconditionally
    home_dir = staging_dir / "home"
    home_dir.mkdir(exist_ok=True)
    project_dir = staging_dir / "project"
    project_dir.mkdir(exist_ok=True)

    def resolve_staged_path(dest_str: str) -> Path:
        """Resolve a destination string to a staged path under home/ or project/."""
        if dest_str.startswith("~"):
            return home_dir / dest_str.removeprefix("~/")
        return project_dir / dest_str

    for dest_path, source in deploy_files.items():
        staged_path = resolve_staged_path(str(dest_path))
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(source, Path):
            shutil.copy2(source, staged_path)
        else:
            staged_path.write_text(source)

    if deploy_files:
        logger.info("Staged {} deploy files from plugins", len(deploy_files))

    # Stage user-specified uploads
    for source_path, dest in uploads:
        staged_path = resolve_staged_path(str(dest))
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_dir():
            shutil.copytree(source_path, staged_path, dirs_exist_ok=True)
        else:
            shutil.copy2(source_path, staged_path)
        logger.debug("Staged upload {} -> {}", source_path, dest)

    if uploads:
        logger.info("Staged {} user-specified uploads", len(uploads))

    # Consolidate environment variables from all sources into a single .env file.
    # Precedence (lowest to highest): --env-file < --pass-env < plugin env vars
    secrets_dir = staging_dir / "secrets"
    secrets_dir.mkdir(exist_ok=True)
    _stage_consolidated_env(secrets_dir, mng_ctx=mng_ctx, pass_env=pass_env, env_files=env_files)


@pure
def _format_env_line(key: str, value: str) -> str:
    """Format a key-value pair as a dotenv line with double-quoted value.

    Double-quoting preserves values that would otherwise be misinterpreted
    by dotenv parsers (e.g. values containing ' # ' are treated as inline
    comments when unquoted). Backslashes and double quotes within the value
    are escaped so they survive a round-trip through dotenv_values().
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"'


def _stage_consolidated_env(
    secrets_dir: Path,
    mng_ctx: MngContext,
    pass_env: Sequence[str] = (),
    env_files: Sequence[Path] = (),
) -> None:
    """Consolidate env vars from multiple sources into secrets/.env.

    Sources are merged in order of increasing precedence:
    1. User-specified --env-file entries (in order)
    2. User-specified --pass-env variables from the current process environment
    3. Plugin mutations via the modify_env_vars_for_deploy hook
    """
    env_dict: dict[str, str] = {}

    # 1. User-specified env files (parsed with dotenv for correct handling
    # of quoting, comments, 'export' prefix, etc.)
    for env_file_path in env_files:
        parsed = dotenv_values(env_file_path)
        for key, value in parsed.items():
            if value is not None:
                env_dict[key] = value
        logger.info("Including env file {}", env_file_path)

    # 2. Pass-through env vars from current process (highest user precedence,
    # overrides env file values)
    for var_name in pass_env:
        value = os.environ.get(var_name)
        if value is not None:
            env_dict[var_name] = value
            logger.debug("Passing through env var {}", var_name)
        else:
            logger.warning("Environment variable '{}' not set in current environment, skipping", var_name)

    # 3. Let plugins mutate the env dict (highest precedence)
    pre_plugin_keys = set(env_dict)
    mng_ctx.pm.hook.modify_env_vars_for_deploy(mng_ctx=mng_ctx, env_vars=env_dict)
    post_plugin_keys = set(env_dict)
    added = post_plugin_keys - pre_plugin_keys
    removed = pre_plugin_keys - post_plugin_keys
    if added or removed:
        logger.info("Plugins modified env vars (added: {}, removed: {})", len(added), len(removed))

    if env_dict:
        final_lines = [_format_env_line(key, value) for key, value in env_dict.items()]
        (secrets_dir / ".env").write_text("\n".join(final_lines) + "\n")
        logger.info("Staged consolidated env file with {} variable entries", len(env_dict))
        # also write to json so it's easier for us to load from the modal function:
        (secrets_dir / "env.json").write_text(json.dumps({key: str(value) for key, value in env_dict.items()}))


@pure
def build_deploy_config(
    app_name: str,
    trigger: ScheduleTriggerDefinition,
    cron_schedule: str,
    cron_timezone: str,
    target_repo_path: str,
    auto_merge_branch: str | None,
) -> dict[str, Any]:
    """Build the deploy configuration dict that gets baked into the Modal image."""
    return {
        "app_name": app_name,
        "trigger": json.loads(trigger.model_dump_json()),
        "cron_schedule": cron_schedule,
        "cron_timezone": cron_timezone,
        "target_repo_path": target_repo_path,
        "auto_merge_branch": auto_merge_branch,
    }


def _save_schedule_creation_record(
    record: ModalScheduleCreationRecord,
    provider: ModalProviderInstance,
) -> None:
    """Save a schedule creation record to the provider's state volume."""
    volume = provider.get_state_volume()
    path = f"{_SCHEDULE_RECORDS_PREFIX}/{record.trigger.name}.json"
    data = record.model_dump_json(indent=2).encode("utf-8")
    volume.write_files({path: data})
    logger.debug("Saved schedule creation record to {}", path)


def list_schedule_creation_records(
    provider: ModalProviderInstance,
) -> list[ModalScheduleCreationRecord]:
    """Read all schedule creation records from the provider's state volume.

    Returns an empty list if no schedules directory exists on the volume.
    """
    volume = provider.get_state_volume()

    try:
        entries = volume.listdir(_SCHEDULE_RECORDS_PREFIX)
    except (modal.exception.NotFoundError, FileNotFoundError):
        return []

    records: list[ModalScheduleCreationRecord] = []
    for entry in entries:
        if not entry.path.endswith(".json"):
            continue
        file_path = f"{_SCHEDULE_RECORDS_PREFIX}/{entry.path}"
        try:
            data = volume.read_file(file_path)
        except (modal.exception.NotFoundError, FileNotFoundError, OSError) as exc:
            logger.warning("Skipped unreadable schedule record at {}: {}", file_path, exc)
            continue
        try:
            record = ModalScheduleCreationRecord.model_validate_json(data)
        except (ValidationError, ValueError) as exc:
            logger.warning("Skipped invalid schedule record at {}: {}", file_path, exc)
            continue
        records.append(record)
    return records


@pure
def _build_full_commandline(sys_argv: list[str]) -> str:
    """Reconstruct the full command line from sys.argv with proper shell escaping."""
    return shlex.join(sys_argv)


def resolve_commit_hash_for_deploy(commit_hash_file: Path, repo_root: Path) -> str:
    if commit_hash_file.exists():
        cached_hash = commit_hash_file.read_text().strip()
        if cached_hash:
            logger.info("Using cached commit hash: {}", cached_hash)
            return cached_hash

    # Resolve HEAD to full SHA
    commit_hash = resolve_git_ref("HEAD", cwd=repo_root)

    # Verify the branch is pushed before caching
    ensure_current_branch_is_pushed(cwd=repo_root)

    # Cache for future builds
    commit_hash_file.write_text(commit_hash)

    raise UserInputError(
        "No cached commit was found, so created one. See output of git diff, add the file, commit, and try again"
    )


def deploy_schedule(
    trigger: ScheduleTriggerDefinition,
    mng_ctx: MngContext,
    provider: ModalProviderInstance,
    verify_mode: VerifyMode = VerifyMode.NONE,
    sys_argv: list[str] | None = None,
    include_user_settings: bool = True,
    include_project_settings: bool = True,
    pass_env: Sequence[str] = (),
    env_files: Sequence[Path] = (),
    uploads: Sequence[tuple[Path, str]] = (),
    mng_install_mode: MngInstallMode = MngInstallMode.AUTO,
    target_repo_path: str = _DEFAULT_TARGET_REPO_PATH,
    auto_merge_branch: str | None = None,
    is_full_copy: bool = False,
) -> str:
    """Deploy a scheduled trigger to Modal, optionally verifying it works.

    The image is built in two stages:
    1. Base image: built from the mng Dockerfile, which provides a complete
       environment with system deps, Python, uv, Claude Code, and mng installed.
       For EDITABLE mode, the mng monorepo tarball is used as the build context.
       For PACKAGE mode, a modified Dockerfile installs mng from PyPI instead.
    2. Target repo layer: the user's project is packaged as a tarball and
       extracted to target_repo_path (default /code/project), with WORKDIR set
       to that location.

    Code packaging modes (controlled by is_full_copy):
    - Incremental (default): resolves a cached git commit hash and packages
      the repo at that commit. Requires a git repo with a pushed branch.
    - Full copy (is_full_copy=True): packages the project at the current HEAD
      commit (if in a git repo, which excludes gitignored files like venvs)
      or tarballs the entire directory (if not in a git repo). Skips the
      incremental caching and branch-push validation.

    Full deployment flow:
    1. Find project root (git root, or cwd for full-copy outside a git repo)
    2. Resolve mng install mode (auto-detect if needed)
    3. Package target repo (incremental: via git commit, full-copy: entire directory)
    4. Build the mng base image (EDITABLE: package monorepo, PACKAGE: modified Dockerfile)
    5. Stage deploy files (collected from plugins via hook) and env vars
    6. Write deploy config as a single JSON file
    7. Run modal deploy cron_runner.py with --env for the correct Modal environment
    8. If verify_mode is not NONE, invoke the function once via modal run to verify
    9. Save creation record to the provider's state volume
    10. Return the Modal app name

    Raises ScheduleDeployError if any step fails.
    """
    # FIXME: we really should have a source repo path in the CLI that is passed through into here (not just assuming it is the current directory), eg, these defaults should happen at a higher level
    # Resolve the project root directory.
    # In full-copy mode, fall back to cwd if not in a git repo.
    if is_full_copy:
        maybe_git_root = try_get_repo_root()
        repo_root = maybe_git_root or Path.cwd()
    else:
        maybe_git_root = get_repo_root()
        repo_root = maybe_git_root

    app_name = get_modal_app_name(trigger.name)
    cron_timezone = detect_local_timezone()
    modal_env_name = provider.environment_name

    repo_root_hash = hashlib.md5(str(repo_root.absolute()).encode("utf-8")).hexdigest()
    deploy_build_path = Path(os.path.expanduser(mng_ctx.config.default_host_dir)) / "build" / repo_root_hash
    deploy_build_path.mkdir(parents=True, exist_ok=True)

    # Resolve mng install mode (auto-detect if needed)
    resolved_install_mode = resolve_mng_install_mode(mng_install_mode)
    logger.info("mng install mode: {}", resolved_install_mode.value.lower())

    logger.info("Deploying schedule '{}' (app: {}, env: {})", trigger.name, app_name, modal_env_name)

    # --- Resolve and package the target repo ---
    target_repo_dir: Path | None = deploy_build_path / "target_repo"

    # FIXME: obviously full copy should be the default, please adjust CLI, docs, and code to account for that
    if is_full_copy:
        # Full-copy mode: skip the incremental caching and branch-push validation.
        # If in a git repo, export at current HEAD (excludes gitignored files like
        # venvs and node_modules). Otherwise, tar the whole directory.
        if maybe_git_root is not None:
            # FIXME: we should just complain for now (raise an exception) if the git repo is not completely clean (no uncommitted or untracked changes)
            head_hash = resolve_git_ref("HEAD", cwd=repo_root)
            trigger = trigger.model_copy(update={"git_image_hash": head_hash})
            logger.info("Full-copy from git repo at HEAD ({})", head_hash)
            with log_span("Packaging repo at HEAD {} (full copy)", head_hash):
                package_repo_at_commit(head_hash, target_repo_dir, repo_root)
        else:
            logger.info("Full-copy from non-git directory {}", repo_root)
            with log_span("Packaging project directory (full copy)"):
                package_directory_as_tarball(repo_root, target_repo_dir)
    else:
        # Incremental mode: resolve commit hash and package via git.
        commit_hash = resolve_commit_hash_for_deploy(repo_root / ".mng" / "image_commit_hash", repo_root)
        trigger = trigger.model_copy(update={"git_image_hash": commit_hash})
        logger.info("Using commit {} for target repo packaging", commit_hash)
        with log_span("Packaging target repo at commit {}", commit_hash):
            package_repo_at_commit(commit_hash, target_repo_dir, repo_root)

    target_tarball = target_repo_dir / "current.tar.gz"
    if not target_tarball.exists():
        raise ScheduleDeployError(
            f"Expected tarball at {target_tarball} after packaging target repo, but it was not found"
        ) from None

    # Ensure the Modal environment exists (modal deploy does not auto-create it)
    _ensure_modal_environment(modal_env_name)

    # --- Build the mng base image context ---
    # For EDITABLE: package the mng monorepo as the build context for the mng Dockerfile.
    # For PACKAGE: use a modified Dockerfile that installs mng from PyPI (no monorepo needed).
    mng_dockerfile_path = get_mng_dockerfile_path(resolved_install_mode)

    # Stage deploy files (collected from plugins via hook)
    staging_dir = deploy_build_path / "staging"
    with log_span("Staging deploy files"):
        stage_deploy_files(
            staging_dir,
            mng_ctx,
            repo_root,
            include_user_settings=include_user_settings,
            include_project_settings=include_project_settings,
            pass_env=pass_env,
            env_files=env_files,
            uploads=uploads,
        )

    mng_build_dir = deploy_build_path / "mng_build"
    mng_build_dir.mkdir(parents=True, exist_ok=True)

    if resolved_install_mode == MngInstallMode.SKIP:
        effective_dockerfile_path = mng_dockerfile_path
        mng_build_dir = target_repo_dir
        target_repo_dir = None
    elif resolved_install_mode == MngInstallMode.EDITABLE:
        mng_repo_root = _get_mng_repo_root()
        mng_head_commit = resolve_git_ref("HEAD", cwd=mng_repo_root)
        with log_span("Packaging mng monorepo at commit {}", mng_head_commit):
            package_repo_at_commit(mng_head_commit, mng_build_dir, mng_repo_root)
        mng_tarball = mng_build_dir / "current.tar.gz"
        if not mng_tarball.exists():
            raise ScheduleDeployError(
                f"Expected tarball at {mng_tarball} after packaging mng monorepo, but it was not found"
            ) from None
        effective_dockerfile_path = mng_dockerfile_path
    else:
        # PACKAGE mode: generate a modified Dockerfile that installs mng from PyPI
        mng_dockerfile_content = mng_dockerfile_path.read_text()
        package_mode_content = _build_package_mode_dockerfile(mng_dockerfile_content)
        effective_dockerfile_path = mng_build_dir / "Dockerfile.package"
        effective_dockerfile_path.write_text(package_mode_content)
        logger.info("Generated PACKAGE mode Dockerfile at {}", effective_dockerfile_path)

    # Validate that GH_TOKEN will be available at runtime when auto-merge is enabled.
    # It must be present either in the consolidated env (via --pass-env or --env-file)
    # or already staged into the secrets directory.
    if auto_merge_branch is not None:
        secrets_env_path = staging_dir / "secrets" / "env.json"
        has_gh_token = False
        if secrets_env_path.exists():
            staged_env = json.loads(secrets_env_path.read_text())
            has_gh_token = "GH_TOKEN" in staged_env
        if not has_gh_token:
            raise ScheduleDeployError(
                "Auto-merge is enabled but no GH_TOKEN was found in the deployed "
                "environment. Pass it via --pass-env GH_TOKEN or include it in an --env-file."
            )

    # Write deploy config as a single JSON file into the staging dir
    deploy_config = build_deploy_config(
        app_name=app_name,
        trigger=trigger,
        cron_schedule=trigger.schedule_cron,
        cron_timezone=cron_timezone,
        target_repo_path=target_repo_path,
        auto_merge_branch=auto_merge_branch,
    )
    deploy_config_json = json.dumps(deploy_config)
    (staging_dir / "deploy_config.json").write_text(deploy_config_json)

    # Build env vars: deploy config as single JSON + local-only paths for image building
    env = os.environ.copy()
    env["SCHEDULE_DEPLOY_CONFIG"] = deploy_config_json
    env["SCHEDULE_BUILD_CONTEXT_DIR"] = str(mng_build_dir)
    env["SCHEDULE_STAGING_DIR"] = str(staging_dir)
    env["SCHEDULE_DOCKERFILE"] = str(effective_dockerfile_path)
    if target_repo_dir:
        env["SCHEDULE_TARGET_REPO_DIR"] = str(target_repo_dir)

    cron_runner_path = Path(__file__).parent / "cron_runner.py"
    cmd = ["uv", "run", "modal", "deploy", "--env", modal_env_name, str(cron_runner_path)]

    with log_span("Deploying to Modal as app '{}' in env '{}'", app_name, modal_env_name):
        with ConcurrencyGroup(name=f"modal-deploy-{trigger.name}") as cg:
            result = cg.run_process_to_completion(
                cmd,
                timeout=600.0,
                env=env,
                is_checked_after=False,
                on_output=_forward_output,
            )
        if result.returncode != 0:
            raise ScheduleDeployError(
                f"Failed to deploy schedule '{trigger.name}' to Modal "
                f"(exit code {result.returncode}). See output above for details."
            ) from None

    logger.info("Schedule '{}' deployed to Modal app '{}'", trigger.name, app_name)

    # FIXME: split this verification logic out and up a layer, this function is already more complicated than necessary
    # Post-deploy verification (must happen while temp dir is still alive)
    if verify_mode != VerifyMode.NONE:
        is_finish = verify_mode == VerifyMode.FULL
        with log_span("Verifying deployment of schedule '{}'", trigger.name):
            verify_schedule_deployment(
                trigger_name=trigger.name,
                modal_env_name=modal_env_name,
                is_finish_initial_run=is_finish,
                env=env,
                cron_runner_path=cron_runner_path,
                mng_ctx=mng_ctx,
            )

    # Save the creation record to the provider's state volume.
    # This is best-effort: the deploy already succeeded, so a failure here
    # should not cause the command to report failure.
    effective_sys_argv = sys_argv if sys_argv is not None else []
    with log_span("Saving schedule creation record"):
        creation_record = ModalScheduleCreationRecord(
            trigger=trigger,
            full_commandline=_build_full_commandline(effective_sys_argv),
            hostname=platform.node(),
            working_directory=str(Path.cwd()),
            mng_git_hash=get_current_mng_git_hash(),
            created_at=datetime.now(timezone.utc),
            app_name=app_name,
            environment=modal_env_name,
        )
        try:
            _save_schedule_creation_record(creation_record, provider)
        except (modal.exception.Error, OSError) as exc:
            logger.warning(
                "Schedule '{}' was deployed successfully but failed to save creation record: {}",
                trigger.name,
                exc,
            )

    return app_name
