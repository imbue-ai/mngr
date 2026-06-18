from imbue.mngr.errors import MngrError


class NotificationsError(MngrError):
    """Base exception for all errors raised by the notifications plugin.

    Inherits from ``MngrError`` so that, when one of these reaches the CLI, it
    renders as a clean ``Error: ...`` message instead of a traceback.
    """


class MalformedAgentStateEventError(NotificationsError, KeyError):
    """Raised when an AGENT_STATE_CHANGE event is missing a required field.

    Required fields (``agent_id``, ``agent_name``) are guaranteed to be present
    on well-formed records emitted by ``mngr observe``; a missing one indicates
    upstream corruption and must fail loudly rather than fabricate an identity.
    """


class MisconfiguredPluginError(NotificationsError, TypeError):
    """Raised when the value registered under the notifications config key is the wrong type.

    ``register_plugin_config("notifications", NotificationsPluginConfig)`` ties the
    ``notifications`` key to exactly ``NotificationsPluginConfig``, so a mismatch
    points at a real registration/parsing bug rather than user input.
    """


class UnsupportedPlatformError(NotificationsError):
    """Raised when desktop notifications are not supported on the current platform."""
