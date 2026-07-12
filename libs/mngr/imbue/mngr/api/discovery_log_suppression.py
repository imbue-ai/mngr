import threading

from loguru import logger
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.api.discovery_events import DiscoveryErrorEvent
from imbue.mngr.api.discovery_events import ProviderDiscoverySnapshotEvent


@pure
def _is_provider_level_error(event: DiscoveryErrorEvent) -> bool:
    """Whether the error is attributed to a provider's discovery as a whole.

    Host- and agent-attributed errors also carry a ``provider_name`` (so the
    aggregator can map them to a provider), but their ``source_name`` is the
    failing host/agent rather than the provider itself. Only whole-provider
    failures have a reliable recovery signal (a clean snapshot from that
    provider), so only they are eligible for suppression.
    """
    return event.provider_name is not None and event.source_name == event.provider_name


class DiscoveryErrorLogSuppressor(MutableModel):
    """Per-process deduplication of provider-level discovery-error log lines.

    A provider stuck in the same failure (e.g. missing credentials) re-emits an
    identical ``DISCOVERY_ERROR`` event on every discovery cycle; logging each
    one drowns the log in repeats. Route every discovery-error event through
    :meth:`log_discovery_error_event` and every per-provider snapshot through
    :meth:`record_provider_snapshot`: a provider-level error is logged once and
    then suppressed until its outcome changes -- a *different* error logs
    immediately, and a clean snapshot from the provider logs a recovery line
    and re-arms suppression. Errors not attributable to a whole provider
    (host/agent failures) are always logged, since their recovery cannot be
    reliably detected.

    Thread-safe; use one instance per consumer process.
    """

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # provider name -> (error_type, error_message) of the error last logged for it.
    _logged_error_by_provider_name: dict[str, tuple[str, str]] = PrivateAttr(default_factory=dict)

    def log_discovery_error_event(self, event: DiscoveryErrorEvent) -> None:
        """Log the event's warning unless it repeats the provider's last-logged error."""
        if not _is_provider_level_error(event):
            logger.warning(
                "Discovery error from {}: {} ({})",
                event.source_name,
                event.error_message,
                event.error_type,
            )
            return
        provider_str = str(event.provider_name)
        error_key = (event.error_type, event.error_message)
        with self._lock:
            is_repeat = self._logged_error_by_provider_name.get(provider_str) == error_key
            if not is_repeat:
                self._logged_error_by_provider_name[provider_str] = error_key
        if is_repeat:
            logger.trace("Suppressed repeated discovery error from provider {}", provider_str)
            return
        logger.warning(
            "Discovery error from {}: {} ({}) [suppressing repeats until this provider's discovery outcome changes]",
            event.source_name,
            event.error_message,
            event.error_type,
        )

    def record_provider_snapshot(self, event: ProviderDiscoverySnapshotEvent) -> None:
        """Re-arm suppression on a provider's first clean snapshot, logging a recovery line.

        A snapshot carrying an error leaves the suppression record untouched
        (snapshot errors surface via consumers' provider state, not the log).
        """
        if event.error is not None:
            return
        provider_str = str(event.provider_name)
        with self._lock:
            logged_error = self._logged_error_by_provider_name.pop(provider_str, None)
        if logged_error is not None:
            error_type, error_message = logged_error
            logger.info(
                "Discovery for provider {} recovered (previous error: {} ({}))",
                provider_str,
                error_message,
                error_type,
            )
