"""The ``<system-injected>`` transcript sentinel for automated ``mngr message`` sends.

An automated (non-human) message delivered to an agent via ``mngr message`` -- the
agentic browser fleet telling a queued agent its browser is free, a latchkey
permission-resolution notice, and so on -- arrives in the agent's transcript as an
ordinary user turn, indistinguishable from something the human typed. Left bare, the
transcript UI renders it as a user bubble as if the person had said it.

Wrapping such a message in this sentinel lets the ``system_interface`` transcript
parser recognise it, strip the wrapper, and stamp ``system_source`` on the emitted
``user_message`` event, so the UI can render it collapsed (like a stop-hook feedback
chip) instead of as a bare user turn. This is UI-ONLY: it changes neither how the
message is delivered nor how turns are grouped -- a fleet "your browser is free"
nudge still starts the agent's next turn exactly as before.

The wrapper only prefixes/suffixes the content (it introduces no new newlines), so a
wrapped message is typed into the pane identically to the same content sent unwrapped
(see ``base_agent._send_tmux_literal_keys``); multi-line content is preserved and the
parser matches it with ``re.DOTALL``.

CROSS-REPO CONTRACT: the matching parser lives at
``apps/system_interface/imbue/system_interface/session_parser.py`` in the
default-workspace-template repo (``_SYSTEM_INJECTED_RE`` / ``_strip_system_injected``).
Keep the tag format here in sync with it.
"""

import re

from imbue.mngr.errors import UserInputError

# A ``system_source`` is a short lowercase slug identifying the automated sender
# (e.g. ``browser-fleet``). The frontend maps it to a display label; an unknown
# slug is title-cased, so any well-formed slug renders sensibly without a code
# change on the UI side.
SYSTEM_INJECTED_SOURCE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def wrap_system_injected(text: str, source: str) -> str:
    """Wrap ``text`` in the ``<system-injected>`` sentinel tagged with ``source``.

    Raises ``UserInputError`` if ``source`` is not a lowercase slug.
    """
    if not SYSTEM_INJECTED_SOURCE_PATTERN.match(source):
        raise UserInputError(f"Invalid system-source {source!r}: expected a lowercase slug like 'browser-fleet'.")
    return f'<system-injected source="{source}">{text}</system-injected>'
