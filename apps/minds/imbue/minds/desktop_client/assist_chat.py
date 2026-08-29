"""The in-workspace ``/assist`` chat that helps diagnose a problem: what the app asks it to do."""

from typing import Final

# Probed (via ``skill_chat``) before spawning: sending ``/assist <text>`` to a
# workspace whose template predates the skill makes Claude reject the unknown
# slash command, which never submits a prompt, so the ``mngr create --message``
# send hangs to its full timeout.
ASSIST_SKILL_NAME: Final[str] = "assist"


def build_assist_chat_message(description: str) -> str:
    """The seed prompt for an assist chat about ``description``."""
    return f"/assist {description}"
