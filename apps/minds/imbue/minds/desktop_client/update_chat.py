"""The in-workspace ``/update-self`` chat that performs an update: what the app asks it to do.

The workspace's update program (``update_workspace_command``) creates the chat and writes its
first message from these parts, whether the run was scheduled or pressed: the lead is told the
target and the app's ceiling and never phones home for them. The skill records its own chat
name in ``run.json``, which is what the app's probes read, so a hand-launched run is
identified the same way.
"""

from typing import Final

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
