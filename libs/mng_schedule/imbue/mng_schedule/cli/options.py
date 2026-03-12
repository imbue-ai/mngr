from imbue.mng.config.data_types import CommonCliOptions


class ScheduleUpdateCliOptions(CommonCliOptions):
    """Shared options for the schedule add and update subcommands."""

    positional_name: str | None
    name: str | None
    command: str | None
    args: str | None
    schedule_cron: str | None
    provider: str | None
    enabled: bool | None
    auto_merge: bool
    auto_merge_branch: str | None
    verify: str
    snapshot_id: str | None
    full_copy: bool
    mng_install_mode: str
    target_dir: str
    include_user_settings: bool | None
    include_project_settings: bool | None
    pass_env: tuple[str, ...]
    env_files: tuple[str, ...]
    uploads: tuple[str, ...]


class ScheduleAddCliOptions(ScheduleUpdateCliOptions):
    """Options for the schedule add subcommand.

    Extends the shared update options with add-specific flags: --update (to allow
    overwriting), --auto-fix-args (to auto-add helpful flags to create commands),
    and --ensure-safe-commands (to error on unsafe command patterns).
    Name is optional here (unlike update) because a random name can be generated.
    """

    name: str | None
    update: bool
    auto_fix_args: bool
    ensure_safe_commands: bool


class ScheduleRemoveCliOptions(CommonCliOptions):
    """Options for the schedule remove subcommand."""

    names: tuple[str, ...]
    force: bool


class ScheduleListCliOptions(CommonCliOptions):
    """Options for the schedule list subcommand."""

    all_schedules: bool
    provider: str


class ScheduleRunCliOptions(CommonCliOptions):
    """Options for the schedule run subcommand."""

    name: str
    local: bool
