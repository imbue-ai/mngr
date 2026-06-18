from enum import auto

from imbue.imbue_common.enums import UpperCaseStrEnum


class AgentUpdatePolicy(UpperCaseStrEnum):
    """How mngr handles an agent CLI's self-update behavior at provision.

    Every supported agent CLI ships its own auto-updater (a background self-update
    or a startup "update available" check). For a managed agent that pins a
    version, an uncontrolled self-update silently moves the binary off the pin, so
    mngr exposes a single knob to govern it. The concrete mechanism differs per
    agent (an env var for claude/antigravity/pi, a config key for codex/opencode),
    but the policy is shared:

    - ``AUTO``  -- leave the CLI's own auto-updater enabled; the CLI may update
      itself as it normally would.
    - ``ASK``   -- gate updates through an interactive prompt at provision time.
      Only some agents implement a prompt flow (codex does); agents without one
      treat ``ASK`` the same as ``AUTO``.
    - ``NEVER`` -- block the CLI's auto-updater so the installed version stays put.

    The default is resolved by ``resolve_update_policy`` rather than being a fixed
    value, because the right default depends on whether the agent runs unattended.
    """

    AUTO = auto()
    ASK = auto()
    NEVER = auto()


def resolve_update_policy(
    configured: AgentUpdatePolicy | None,
    *,
    is_unattended: bool,
    is_ask_capable: bool,
) -> AgentUpdatePolicy:
    """Resolve a possibly-unset update policy to a concrete one.

    A user's explicit choice always wins. When unset (``None``), the default is:

    - ``NEVER`` when the agent runs unattended -- a remote/deploy agent has no one
      watching, so it must not self-update mid-run (and an update could break a
      pinned-version reproduction).
    - otherwise ``ASK`` when the agent implements an interactive update flow, so an
      attended user is prompted before anything changes;
    - otherwise ``AUTO`` -- with no prompt flow available, fall back to the CLI's
      own default behavior rather than silently freezing it.
    """
    if configured is not None:
        return configured
    if is_unattended:
        return AgentUpdatePolicy.NEVER
    return AgentUpdatePolicy.ASK if is_ask_capable else AgentUpdatePolicy.AUTO
