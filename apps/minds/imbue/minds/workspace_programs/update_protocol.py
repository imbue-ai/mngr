from typing import Final

# The update program's state, relative to the workspace root, where ``mngr exec`` lands and cron's tick runs.
UPDATE_STATE_DIR: Final[str] = "data/.state/update-apply"
RUN_RECORD_PATH: Final[str] = f"{UPDATE_STATE_DIR}/run.json"
SCHEDULE_FILE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/schedule.json"
LAST_DISPATCH_FILE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/last-dispatch.json"

# The cron drop-in that runs the dispatcher's tick, in the directory the cron daemon reads.
DISPATCHER_CRON_NAME: Final[str] = "update-self-dispatch"
LIVE_CRON_DIRECTORY: Final[str] = "/etc/cron.d"

# Opens the program's one JSON answer on stdout (see ``program_support.emit_marker_json``).
UPDATE_COMMAND_RESULT_MARKER: Final[str] = "MINDS_UPDATE_COMMAND_JSON:"

# Asks the command's ``read`` whether an account is signed in.
UPDATE_COMMAND_CHECK_SIGN_IN_FLAG: Final[str] = "--check-sign-in"

# How a run was started, as the program stamps it in ``last-dispatch.json`` (the app's ``UpdateStartedBy``,
# lower-cased).
STARTED_BY_SCHEDULED: Final[str] = "scheduled"
STARTED_BY_IMMEDIATE: Final[str] = "immediate"

# What one run of an ``arm`` or ``now`` came to (the app's ``UpdateArmOutcome``).
OUTCOME_ARMED: Final[str] = "ARMED"
OUTCOME_CREATED: Final[str] = "CREATED"
OUTCOME_ALREADY_RUNNING: Final[str] = "ALREADY_RUNNING"
OUTCOME_NEEDS_SIGN_IN: Final[str] = "NEEDS_SIGN_IN"
OUTCOME_CHAT_APP_UNREACHABLE: Final[str] = "CHAT_APP_UNREACHABLE"
OUTCOME_CREATE_FAILED: Final[str] = "CREATE_FAILED"
OUTCOME_NO_CRON: Final[str] = "NO_CRON"
OUTCOME_TOO_OLD_TO_UPDATE: Final[str] = "TOO_OLD_TO_UPDATE"

# Why a window closed without its scheduled run starting (the app's ``UpdateSkipReason``).
SKIP_UPDATE_IN_FLIGHT: Final[str] = "UPDATE_IN_FLIGHT"
SKIP_LEASE_HELD: Final[str] = "LEASE_HELD"
SKIP_NEEDS_SIGN_IN: Final[str] = "NEEDS_SIGN_IN"
SKIP_DISPATCH_FAILED: Final[str] = "DISPATCH_FAILED"
SKIP_WINDOW_CLOSED: Final[str] = "WINDOW_CLOSED"

# The update chat's first message, whether the run was scheduled or pressed: the lead is told the target
# and the app's ceiling and never phones home for them. The skill records its own chat name in
# ``run.json``, which is what the app's probes read, so a hand-launched run is identified the same way.
UPDATE_COMMAND: Final[str] = "/update-self"

# Read by the skill's Step 2 from the release that ships the pinned-target prose on.
PINNED_NOTE_TEMPLATE: Final[str] = (
    "The Imbue Studio app pinned this update, so do not ask it for its version:\n"
    "- Target: {target}\n"
    "- Ceiling: {ceiling}"
)

# CLEANUP: drop this instruction once the oldest release the app updates in place
# (``OLDEST_IN_PLACE_UPDATABLE_VERSION``) ships update-self prose whose Step 2 reads the pinned
# lines above itself: until then a workspace's local Step 2 predates them, and ``--ceiling`` is
# the flag every release back to minds-v0.3.10 takes.
OLD_STEP_TWO_INSTRUCTION_TEMPLATE: Final[str] = (
    "When you resolve the target in Step 2, append `--ceiling {ceiling}` to the `resolve-target` command. "
    "If the app cannot be reached for anything else during this run, that is not a fault."
)

# Names the target and that the user already confirmed it, but no flag or step: the skill
# re-points itself at the target version's own copy of its flow, so the mechanism may differ.
OVERRIDE_NOTE_TEMPLATE: Final[str] = (
    "The user chose a specific version in the Imbue Studio app: update this workspace to {target}. "
    "This is their explicit override, chosen knowing it may be newer than the app or not a "
    "release. Treat it as their confirmation of the target; do not ask them to confirm it again."
)

# CLEANUP: drop this note (and the ``is_backup_configured`` field that selects it) once no
# workspace can still be running an update-self whose preconditions stop to ask about a missing
# restore point -- i.e. once the oldest template an update can be applied to is past the release
# that made the flow unattended.
NO_BACKUP_NOTE: Final[str] = (
    "This workspace has no backups configured, so this update has no restore point to fall back "
    "on. The Imbue Studio app said so before the user started it, and they chose to go ahead without "
    "one. Treat that as their go-ahead; do not stop to ask them about the missing backup."
)

# What the watchdog sends an idle lead whose run has no verdict.
WATCHDOG_REMINDER_MESSAGE: Final[str] = (
    "A note from the Imbue Studio app: this update has no recorded result yet, and neither you nor "
    "its background agent is working on it. Carry on if you can. If the update has ended, record "
    "the verdict the update-self skill calls for, and tell the user; if a fault stopped it and you "
    'cannot recover, that is `run-status verdict STUCK --detail "<why, one plain line>"` from the '
    "update-self script. Until a result is recorded you will keep being reminded."
)

# What the watchdog sends a stopped lead whose run has no verdict; sending it is what restarts the chat.
WATCHDOG_REVIVE_MESSAGE_TEMPLATE: Final[str] = (
    "A note from the Imbue Studio app: this update's chat stopped before the update recorded a result "
    "(the workspace may have restarted), so the app has started it again. Restarts so far: {count}. "
    "Check where the update got to and carry on. If it keeps stopping for a reason you cannot find or "
    'fix, record `run-status verdict STUCK --detail "<why, one plain line>"` from the update-self '
    "script and tell the user."
)

# The verdict detail the watchdog records for a run whose lead's chat was deleted.
WATCHDOG_GONE_LEAD_DETAIL: Final[str] = "The update's chat was deleted before the update finished."
