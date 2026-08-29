"""The in-workspace ``/update-self`` chat that performs an update: what the app asks it to do.

The skill records its own chat name in ``run.json``, which is what the app's
probes read, so a hand-launched run is identified the same way.
"""

from typing import Final

# Probed (via ``skill_chat``) before spawning: a template without the skill
# would accept the create and then hang on the unknown slash command.
UPDATE_SKILL_NAME: Final[str] = "update-self"

_UPDATE_COMMAND: Final[str] = "/update-self"

# Names the target and that the user already confirmed it, but no flag or step: the skill
# re-points itself at the target version's own copy of its flow, so the mechanism may differ.
_OVERRIDE_NOTE_TEMPLATE: Final[str] = (
    "The user chose a specific version in the Minds app: update this workspace to {target}. "
    "This is their explicit override, chosen knowing it may be newer than the app or not a "
    "release. Treat it as their confirmation of the target; do not ask them to confirm it again."
)

# CLEANUP: drop this note (and the ``is_backup_configured`` argument that selects it) once no
# workspace can still be running an update-self whose preconditions stop to ask about a missing
# restore point -- i.e. once the oldest template an update can be applied to is past the release
# that made the flow unattended.
_NO_BACKUP_NOTE: Final[str] = (
    "This workspace has no backups configured, so this update has no restore point to fall back "
    "on. The Minds app said so before the user started it, and they chose to go ahead without "
    "one. Treat that as their go-ahead; do not stop to ask them about the missing backup."
)


def build_update_chat_message(*, target_override: str | None, is_backup_configured: bool) -> str:
    """The seed prompt for an update chat, the command plus whatever the run should not re-ask.

    ``target_override`` names the exact ref the user chose, '' or None for the skill's own default.
    """
    notes: list[str] = []
    if target_override:
        notes.append(_OVERRIDE_NOTE_TEMPLATE.format(target=target_override))
    if not is_backup_configured:
        notes.append(_NO_BACKUP_NOTE)
    return "\n\n".join([_UPDATE_COMMAND, *notes])
