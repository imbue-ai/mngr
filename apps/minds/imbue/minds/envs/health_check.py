"""Post-deploy health-check poller for the two deployed Modal apps.

After ``modal deploy`` succeeds for both apps, `deploy_env` polls
:func:`await_apps_healthy` for up to 30 seconds. Both apps must hit
200 with the expected response shape at least once during the window
before the deploy is considered successful. Failure throws
:class:`HealthCheckFailedError` which the CLI surfaces with the same
"run `minds env recover`" guidance as any other deploy failure.

Categorization (per response):

* **Success**: HTTP 200 with expected shape.
* **Transient (continue polling)**: connection refused / reset, DNS
  not resolving, socket timeout, HTTP 502/503/504 with empty body,
  any HTTP 5xx during the first 10 seconds of the window (cold-boot
  tolerance).
* **Definitive (fail immediately)**: HTTP 4xx, HTTP 5xx with a
  non-empty body after the cold-boot window, malformed response
  (e.g. ``/generation`` returning a SuperTokens login redirect /
  non-JSON), HTTP 200 with the wrong content shape.

The two endpoints checked:

* ``GET <connector_url>/health/liveness`` -- the connector's no-auth
  liveness probe (mirrors the LiteLLM proxy's surface). Returns 200
  with a tiny JSON body the poller can match against. We use this
  instead of ``/docs`` (the FastAPI Swagger UI page) because the
  Swagger UI returns 4 KB of HTML and is mounted via a separate
  ASGI sub-app -- noisier and slightly slower during cold-start.
* ``GET <litellm_proxy_url>/health/liveness`` -- LiteLLM's no-auth
  liveness probe (returns 200 when the process is up). We avoid
  ``/health`` because that endpoint requires a master-key bearer
  token + actually pings every configured model, which is much
  heavier than a "is the proxy responding" check.
"""

from collections.abc import Callable
from time import monotonic
from typing import Final

import httpx
from loguru import logger
from pydantic import AnyUrl

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.errors import MindError
from imbue.mngr.utils.polling import poll_for_value

_DEFAULT_MAX_SECONDS: Final[float] = 60.0
_DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 2.0
_DEFAULT_PER_ATTEMPT_TIMEOUT_SECONDS: Final[float] = 10.0
_DEFAULT_COLD_BOOT_SECONDS: Final[float] = 10.0


class HealthCheckFailedError(MindError):
    """Raised when an app fails its post-deploy health check.

    Catches both "definitive" rejections (4xx, malformed responses) and
    "timed out polling for success" (none of the polls hit 200 within
    the window).
    """


class _DefinitiveHealthCheckFailure(Exception):
    """Internal sentinel raised inside the poll callable to short-circuit polling.

    ``poll_for_value`` retries when the callable returns ``None`` but
    propagates exceptions, so we use this to escape the polling loop
    immediately on a definitive (non-transient) failure.
    """


def _is_transient_status(*, status_code: int, body_is_empty: bool, elapsed_seconds: float) -> bool:
    """Return True iff this HTTP response is transient -> keep polling.

    Cold-boot tolerance: any 4xx or 5xx within the first 10 seconds is
    treated as transient. The 4xx arm exists because Modal can serve a
    stale container from the prior version during the swap window when
    ``min_containers=0`` (dev tier default) -- the new URL is reachable
    but routes to the old code, which returns 404 from FastAPI for
    routes that didn't exist there yet (e.g. a newly-added
    ``/health/liveness``). Without the cold-boot tolerance, the very
    first deploy that adds a new healthcheck path would fail every
    time. After the window, 4xx means the app is up but the route is
    really missing -- treated as definitive. 5xx after the window is
    only treated as transient when the body is empty (502/503/504 from
    Modal's edge usually means the app is still booting; 5xx with a
    body usually means the app is up but broken).
    """
    if status_code in (502, 503, 504) and body_is_empty:
        return True
    if 400 <= status_code < 600 and elapsed_seconds < _DEFAULT_COLD_BOOT_SECONDS:
        return True
    return False


def check_once(
    *,
    client: httpx.Client,
    url: str,
    expected_substring: str | None,
    per_attempt_timeout: float,
    elapsed_seconds: float,
) -> tuple[bool, str | None]:
    """One poll attempt against a URL using an injected ``httpx.Client``.

    Returns ``(True, None)`` on success, ``(False, None)`` on transient
    (keep polling), or ``(False, "reason")`` on definitive failure.

    The ``client`` parameter is injected so tests can pass a
    ``httpx.Client(transport=httpx.MockTransport(...))`` instead of
    making real network calls.
    """
    try:
        response = client.get(url, timeout=per_attempt_timeout)
    except httpx.TimeoutException:
        return (False, None)
    except httpx.ConnectError as exc:
        logger.debug("Health check {!r}: transient connect error ({})", url, exc)
        return (False, None)
    except httpx.HTTPError as exc:
        return (False, f"httpx error: {exc}")

    status = response.status_code
    body = response.text
    body_is_empty = not body.strip()

    if status == 200:
        if expected_substring is None or expected_substring in body:
            return (True, None)
        return (False, f"HTTP 200 but expected substring {expected_substring!r} not in body: {body[:200]!r}")

    if _is_transient_status(status_code=status, body_is_empty=body_is_empty, elapsed_seconds=elapsed_seconds):
        logger.debug(
            "Health check {!r}: transient HTTP {} (body empty={}, elapsed={:.1f}s)",
            url,
            status,
            body_is_empty,
            elapsed_seconds,
        )
        return (False, None)

    return (False, f"HTTP {status} with non-empty body after cold-boot window: {body[:200]!r}")


class _HealthPollCallable(FrozenModel):
    """Single-shot callable wrapper for ``poll_for_value``.

    Returns ``True`` (a sentinel "non-None") on success so the poller
    stops; ``None`` on transient -> poller retries. On a definitive
    failure, raises :class:`_DefinitiveHealthCheckFailure` (its message
    is the categorization reason) so the poller short-circuits.

    ``start`` is captured at instantiation time and re-used across
    every retry so the cold-boot window math measures from "first
    poll" not "this poll".
    """

    model_config = {"arbitrary_types_allowed": True, "frozen": True}

    client: httpx.Client
    url: str
    expected_substring: str | None
    per_attempt_timeout: float
    start: float

    def __call__(self) -> bool | None:
        elapsed = monotonic() - self.start
        ok, reason = check_once(
            client=self.client,
            url=self.url,
            expected_substring=self.expected_substring,
            per_attempt_timeout=self.per_attempt_timeout,
            elapsed_seconds=elapsed,
        )
        if ok:
            return True
        if reason is not None:
            raise _DefinitiveHealthCheckFailure(reason)
        return None


def _poll_until_healthy(
    *,
    client: httpx.Client,
    url: str,
    expected_substring: str | None,
    max_seconds: float,
    poll_interval: float,
    per_attempt_timeout: float,
) -> None:
    """Block until ``url`` returns a healthy 200 or the budget elapses.

    Raises :class:`HealthCheckFailedError` on definitive failure or
    timeout. Uses :func:`poll_for_value` for the sleep loop so the
    polling backoff is owned by one well-tested helper.
    """
    callable_under_test = _HealthPollCallable(
        client=client,
        url=url,
        expected_substring=expected_substring,
        per_attempt_timeout=per_attempt_timeout,
        start=monotonic(),
    )
    try:
        value, poll_count, elapsed = poll_for_value(
            callable_under_test, timeout=max_seconds, poll_interval=poll_interval
        )
    except _DefinitiveHealthCheckFailure as exc:
        raise HealthCheckFailedError(f"Health check {url!r} failed definitively: {exc}") from exc
    if value is None:
        raise HealthCheckFailedError(
            f"Health check {url!r} did not return 200 within {max_seconds:.0f}s "
            f"({poll_count} polls in {elapsed:.1f}s)."
        )


def await_apps_healthy(
    *,
    connector_url: AnyUrl,
    litellm_proxy_url: AnyUrl,
    max_seconds: float = _DEFAULT_MAX_SECONDS,
    poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    per_attempt_timeout: float = _DEFAULT_PER_ATTEMPT_TIMEOUT_SECONDS,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> None:
    """Poll both apps' health endpoints until both return 200 or the budget runs out.

    Sequential, not parallel: connector first, then LiteLLM proxy. Each
    has its own polling budget (``max_seconds`` per app), so a slow
    connector cold-start doesn't eat the LiteLLM proxy's budget.

    ``client_factory`` is injected so tests can pass a factory that
    returns an ``httpx.Client(transport=httpx.MockTransport(...))``;
    production uses a real ``httpx.Client``.

    Raises :class:`HealthCheckFailedError` on any definitive failure or
    on timeout. Returns ``None`` on success.
    """
    connector_health_url = f"{str(connector_url).rstrip('/')}/health/liveness"
    litellm_health_url = f"{str(litellm_proxy_url).rstrip('/')}/health/liveness"

    factory = client_factory if client_factory is not None else httpx.Client
    with factory() as client:
        logger.info("Health check: polling connector at {} (max {}s)", connector_health_url, max_seconds)
        _poll_until_healthy(
            client=client,
            url=connector_health_url,
            expected_substring=None,
            max_seconds=max_seconds,
            poll_interval=poll_interval,
            per_attempt_timeout=per_attempt_timeout,
        )
        logger.info(
            "Health check: connector healthy. Polling litellm-proxy at {} (max {}s)",
            litellm_health_url,
            max_seconds,
        )
        _poll_until_healthy(
            client=client,
            url=litellm_health_url,
            expected_substring=None,
            max_seconds=max_seconds,
            poll_interval=poll_interval,
            per_attempt_timeout=per_attempt_timeout,
        )
        logger.info("Health check: both apps healthy.")
