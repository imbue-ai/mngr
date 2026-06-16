"""Core, agent-agnostic wiring for the ``--adopt-session`` create option.

Session adoption (resume an existing conversation in a freshly created agent) is a
capability declared by ``HasSessionAdoptionMixin`` and detected in the capability matrix,
so the CLI surface that drives it belongs in core rather than in any one agent's plugin.
This module is registered as a built-in plugin (see ``main.py``) and declares the
``--adopt-session`` option plus the agent-agnostic validation that applies to every
adoption-capable agent. Each agent plugin still owns its agent-specific handling: how it
resolves and rebinds the session (in ``adopt_session``), and its own fail-fast
pre-resolution of a named session id (in its own ``on_before_create``).
"""

from collections.abc import Mapping

from imbue.mngr import hookimpl
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.agent import HasSessionAdoptionMixin
from imbue.mngr.plugins.hookspecs import OnBeforeCreateArgs
from imbue.mngr.plugins.hookspecs import OptionStackItem


@hookimpl
def register_cli_options(command_name: str) -> Mapping[str, list[OptionStackItem]] | None:
    """Declare the agent-agnostic ``--adopt-session`` option on ``create``."""
    if command_name != "create":
        return None
    return {
        "Behavior": [
            OptionStackItem(
                param_decls=("--adopt-session",),
                multiple=True,
                help="Adopt an existing session into this newly created agent so it resumes "
                "that conversation. The agent type must support session adoption. Accepts a "
                "session id or a path to the session file; a session id is searched across the "
                "relevant user/config store, every live local mngr agent, and preserved "
                "sessions from destroyed agents. Repeatable: the last named session is the one "
                "resumed on startup.",
            ),
        ]
    }


@hookimpl
def on_before_create(args: OnBeforeCreateArgs, mngr_ctx: MngrContext) -> OnBeforeCreateArgs | None:
    """Validate ``--adopt-session`` against any agent type, before a host/worktree exists.

    Agent-agnostic checks only: the target type must support session adoption, and the
    option is incompatible with cloning via ``--from <agent>`` (both seed the new agent's
    resume session). Each adoption-capable plugin runs its own ``on_before_create`` to
    fail-fast on a bad/ambiguous session id against its native store.
    """
    adopt_session = args.agent_options.plugin_data.get("adopt_session", ())
    if not adopt_session:
        return None

    resolved = resolve_agent_type(args.agent_options.agent_type, mngr_ctx.config)
    if not issubclass(resolved.agent_class, HasSessionAdoptionMixin):
        raise UserInputError(
            f"--adopt-session can only be used with an agent type that supports session adoption, "
            f"not '{args.agent_options.agent_type}'."
        )

    if args.agent_options.source_agent_state_location is not None:
        raise UserInputError(
            "--adopt-session is incompatible with cloning via --from <agent>: both "
            "adopt a session into the new agent. Pick one."
        )

    return None
