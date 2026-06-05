"""Zero-config in-process ``MngrContext`` construction for the mngr-backed Agent SDK.

The real ``claude_agent_sdk`` entry points (``query`` / ``ClaudeSDKClient`` / the session
functions) take no mngr context -- a caller just imports and calls them. To match that, the
SDK builds its own ``MngrContext`` from the user's mngr configuration, mirroring what the CLI's
``setup_command_context`` does but without any click plumbing: create (or reuse) the plugin
manager, open a ``ConcurrencyGroup`` for process management, and load the config.
"""

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.loader import load_config
from imbue.mngr.config.loader import resolve_strict_from_env
from imbue.mngr.main import get_or_create_plugin_manager

# Name for the concurrency group that owns subprocesses spawned while the SDK drives agents.
_CONCURRENCY_GROUP_NAME = "mngr-agent-sdk"


def build_sdk_mngr_context(concurrency_group: ConcurrencyGroup) -> MngrContext:
    """Load the user's mngr configuration into a ``MngrContext`` for headless SDK use.

    The caller owns the passed-in ``ConcurrencyGroup`` (it must already be entered and is
    responsible for exiting it); the SDK ties the group's lifetime to a ``ClaudeSDKClient``
    connection / a single ``query()`` call so spawned processes are always cleaned up.
    """
    plugin_manager = get_or_create_plugin_manager()
    return load_config(
        plugin_manager,
        concurrency_group,
        enabled_plugins=None,
        disabled_plugins=None,
        is_interactive=False,
        strict=resolve_strict_from_env(),
        silent_unknown_fields=False,
    )


def open_sdk_concurrency_group() -> ConcurrencyGroup:
    """Create and enter a ``ConcurrencyGroup`` for one SDK connection / query.

    Mirrors ``setup_command_context``: the group is entered immediately so child processes are
    tracked; the caller must call ``__exit__`` (typically from ``ClaudeSDKClient.disconnect``
    or at the end of ``query()``) to guarantee cleanup.
    """
    concurrency_group = ConcurrencyGroup(name=_CONCURRENCY_GROUP_NAME)
    concurrency_group.__enter__()
    return concurrency_group
