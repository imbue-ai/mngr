"""Local schedule deployment using the system crontab.

Each trigger gets:
1. A directory at {default_host_dir}/schedule/{name}/ containing:
   - run.sh: wrapper script that sets up the environment and runs the mng command
   - .env: optional environment variables (from --pass-env / --env-file)
2. A creation record at {default_host_dir}/schedule/records/{name}.json
3. A crontab entry pointing to the wrapper script
"""

import os
import platform
import shlex
import stat
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.pure import pure
from imbue.mng.config.data_types import MngContext
from imbue.mng_schedule.data_types import ScheduleCreationRecord
from imbue.mng_schedule.data_types import ScheduleTriggerDefinition
from imbue.mng_schedule.env import collect_env_lines
from imbue.mng_schedule.git import get_current_mng_git_hash
from imbue.mng_schedule.implementations.local.crontab import add_crontab_entry
from imbue.mng_schedule.implementations.local.crontab import read_system_crontab
from imbue.mng_schedule.implementations.local.crontab import write_system_crontab

_SCHEDULE_DIR_NAME: Final[str] = "schedule"
_TRIGGERS_DIR_NAME: Final[str] = "triggers"
_RECORDS_DIR_NAME: Final[str] = "records"


def _get_schedule_base_dir(mng_ctx: MngContext) -> Path:
    """Get the base directory for local schedule data.

    Uses the configured default_host_dir (typically ~/.mng/schedule/).
    """
    return mng_ctx.config.default_host_dir.expanduser() / _SCHEDULE_DIR_NAME


def _get_trigger_dir(mng_ctx: MngContext, trigger_name: str) -> Path:
    """Get the directory for a specific trigger's runtime files."""
    return _get_schedule_base_dir(mng_ctx) / _TRIGGERS_DIR_NAME / trigger_name


def _get_records_dir(mng_ctx: MngContext) -> Path:
    """Get the directory for schedule creation records."""
    return _get_schedule_base_dir(mng_ctx) / _RECORDS_DIR_NAME


@pure
def build_wrapper_script(
    trigger: ScheduleTriggerDefinition,
    working_directory: str,
    path_value: str,
    env_file_path: Path | None,
) -> str:
    """Build the contents of the run.sh wrapper script.

    The wrapper script:
    1. Sets PATH from the value captured at schedule creation time
    2. Sources an optional .env file for pass-env/env-file vars
    3. Changes to the working directory
    4. Runs uv run mng <command> <args>
    """
    lines: list[str] = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"export PATH={shlex.quote(path_value)}",
        "",
    ]

    if env_file_path is not None:
        lines.extend(
            [
                f"if [ -f {shlex.quote(str(env_file_path))} ]; then",
                "    set -a",
                f"    source {shlex.quote(str(env_file_path))}",
                "    set +a",
                "fi",
                "",
            ]
        )

    lines.append(f"cd {shlex.quote(working_directory)}")
    lines.append("")

    cmd_parts = ["uv", "run", "mng", trigger.command.value.lower()]
    if trigger.args:
        cmd_parts.extend(shlex.split(trigger.args))
    mng_command = shlex.join(cmd_parts)

    lines.append(f"exec {mng_command}")
    lines.append("")

    return "\n".join(lines)


def _stage_env_file(
    trigger_dir: Path,
    pass_env: Sequence[str],
    env_files: Sequence[Path],
) -> Path | None:
    """Write a consolidated .env file into the trigger directory.

    Returns the path to the .env file, or None if no env vars were provided.
    """
    env_lines = collect_env_lines(pass_env=pass_env, env_files=env_files)

    if not env_lines:
        return None

    env_file = trigger_dir / ".env"
    env_file.write_text("\n".join(env_lines) + "\n")
    logger.info("Wrote consolidated env file with {} entries", len(env_lines))
    return env_file


def _save_creation_record(
    record: ScheduleCreationRecord,
    mng_ctx: MngContext,
) -> None:
    """Save a schedule creation record as a JSON file."""
    records_dir = _get_records_dir(mng_ctx)
    records_dir.mkdir(parents=True, exist_ok=True)
    record_path = records_dir / f"{record.trigger.name}.json"
    record_path.write_text(record.model_dump_json(indent=2))
    logger.debug("Saved schedule creation record to {}", record_path)


def list_local_schedule_creation_records(
    mng_ctx: MngContext,
) -> list[ScheduleCreationRecord]:
    """Read all schedule creation records from the local filesystem.

    Returns an empty list if no records directory exists.
    """
    records_dir = _get_records_dir(mng_ctx)
    if not records_dir.is_dir():
        return []

    records: list[ScheduleCreationRecord] = []
    for record_path in sorted(records_dir.iterdir()):
        if not record_path.name.endswith(".json"):
            continue
        try:
            data = record_path.read_bytes()
        except OSError as exc:
            logger.warning("Skipped unreadable schedule record at {}: {}", record_path, exc)
            continue
        try:
            record = ScheduleCreationRecord.model_validate_json(data)
        except (ValidationError, ValueError) as exc:
            logger.warning("Skipped invalid schedule record at {}: {}", record_path, exc)
            continue
        records.append(record)
    return records


CrontabReader = Callable[[], str]
CrontabWriter = Callable[[str], None]
GitHashResolver = Callable[[], str]


def deploy_local_schedule(
    trigger: ScheduleTriggerDefinition,
    mng_ctx: MngContext,
    sys_argv: Sequence[str] | None = None,
    pass_env: Sequence[str] = (),
    env_files: Sequence[Path] = (),
    crontab_reader: CrontabReader = read_system_crontab,
    crontab_writer: CrontabWriter = write_system_crontab,
    git_hash_resolver: GitHashResolver = get_current_mng_git_hash,
) -> None:
    """Deploy a scheduled trigger to the local system crontab.

    Deployment flow:
    1. Create trigger directory at {default_host_dir}/schedule/{name}/
    2. Stage environment variables into .env file (if any)
    3. Build and write the wrapper script (run.sh)
    4. Read existing crontab, add/update entry, write back
    5. Save creation record

    The crontab_reader, crontab_writer, and git_hash_resolver parameters
    allow substituting test implementations for the system crontab and
    git operations.

    Raises ScheduleDeployError if any step fails.
    """
    trigger_dir = _get_trigger_dir(mng_ctx, trigger.name)
    trigger_dir.mkdir(parents=True, exist_ok=True)

    working_directory = str(Path.cwd())
    path_value = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")

    # Stage env file
    env_file_path = _stage_env_file(trigger_dir, pass_env=pass_env, env_files=env_files)

    # Build and write wrapper script
    script_content = build_wrapper_script(
        trigger=trigger,
        working_directory=working_directory,
        path_value=path_value,
        env_file_path=env_file_path,
    )
    run_script = trigger_dir / "run.sh"
    run_script.write_text(script_content)
    run_script.chmod(run_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    logger.info("Created wrapper script at {}", run_script)

    # Update crontab
    prefix = mng_ctx.config.prefix
    existing_crontab = crontab_reader()
    updated_crontab = add_crontab_entry(
        existing_content=existing_crontab,
        prefix=prefix,
        trigger_name=trigger.name,
        cron_expression=trigger.schedule_cron,
        command=str(run_script),
    )
    crontab_writer(updated_crontab)

    logger.info("Installed crontab entry for schedule '{}'", trigger.name)

    # Save creation record
    effective_sys_argv = list(sys_argv) if sys_argv is not None else []
    creation_record = ScheduleCreationRecord(
        trigger=trigger,
        full_commandline=shlex.join(effective_sys_argv),
        hostname=platform.node(),
        working_directory=working_directory,
        mng_git_hash=git_hash_resolver(),
        created_at=datetime.now(timezone.utc),
    )
    _save_creation_record(creation_record, mng_ctx)
