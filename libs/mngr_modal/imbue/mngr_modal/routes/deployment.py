import hashlib
from pathlib import Path
from typing import Final

from loguru import logger
from tenacity import RetryCallState
from tenacity import Retrying
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import stop_after_delay
from tenacity import wait_exponential

from imbue.imbue_common.logging import log_span
from imbue.mngr.errors import MngrError
from imbue.modal_proxy.direct import DEPLOY_ATTEMPT_TIMEOUT_SECONDS
from imbue.modal_proxy.errors import ModalProxyDeployRetryableError
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.errors import ModalProxyNotFoundError
from imbue.modal_proxy.interface import ModalInterface

# Prefix of the marker function each deployed route script publishes alongside
# its endpoint, completed by a digest of the script it was deployed from. Route
# scripts are handed the finished name through MNGR_MODAL_SOURCE_MARKER_NAME; a
# script that publishes no marker just gets deployed on every call.
_SOURCE_MARKER_FUNCTION_PREFIX: Final[str] = "deployed_source_"
# Enough digest to make a collision between two versions of one script
# implausible, while keeping the deployed function name readable.
_SOURCE_MARKER_DIGEST_LENGTH: Final[int] = 16

# How long to keep re-attempting a deploy Modal failed without judging it.
# Bounded by elapsed time rather than by attempts, because the longest-running
# of these failures is contention: Modal locks an app for the duration of a
# mutation, so concurrent deploys of one app name race and the losers are
# refused, and CI fans dozens of creates out against one shared app name while
# several CI runs do the same. The lock can therefore stay held for minutes
# while the queue drains, and a deploy that is merely queued must keep waiting
# rather than fail.
DEPLOY_RETRY_BUDGET_SECONDS: Final[float] = 300.0
# A deploy takes several seconds, so retrying sooner than this just wastes
# attempts -- and a wasted attempt against a locked app holds the lock against
# the rest of the queue.
DEPLOY_RETRY_MIN_BACKOFF_SECONDS: Final[float] = 2.0
DEPLOY_RETRY_MAX_BACKOFF_SECONDS: Final[float] = 15.0
# Upper bound on how long ensure_function_deployed can block. The budget only
# gates whether another attempt starts, so the last attempt can begin just
# under it (after a full backoff) and still run to the deploy subprocess's own
# timeout.
DEPLOY_MAX_DURATION_SECONDS: Final[float] = (
    DEPLOY_RETRY_BUDGET_SECONDS + DEPLOY_RETRY_MAX_BACKOFF_SECONDS + DEPLOY_ATTEMPT_TIMEOUT_SECONDS
)


def _log_deploy_retry(retry_state: RetryCallState) -> None:
    """Name the failure each re-attempt is waiting out.

    A create blocks here for minutes without touching the host it is building,
    and the wait is reported from the other side (``await_snapshot_endpoint_url``)
    without knowing which of these failures is behind it.
    """
    outcome = retry_state.outcome
    logger.warning(
        "Modal failed the deploy ({}); re-attempting (attempt {}, {:.0f}s spent)",
        outcome.exception() if outcome is not None else "unknown",
        retry_state.attempt_number,
        retry_state.seconds_since_start or 0.0,
    )


def get_route_script_path(function: str) -> Path:
    """The standalone script that publishes a route function."""
    return Path(__file__).parent / f"{function}.py"


def get_source_marker_function_name(function: str) -> str:
    """Name of the marker published by the current source of a route script.

    Derived from the script's bytes, which (these scripts being standalone, by
    their own contract) completely determine what gets deployed.
    """
    digest = hashlib.sha256(get_route_script_path(function).read_bytes()).hexdigest()
    return f"{_SOURCE_MARKER_FUNCTION_PREFIX}{digest[:_SOURCE_MARKER_DIGEST_LENGTH]}"


def _url_if_app_already_carries_source(
    function: str,
    app_name: str,
    environment_name: str,
    modal_interface: ModalInterface,
) -> str | None:
    """The function's URL if the app already carries this exact source, else None.

    Every way of failing to establish that the app already carries this source
    answers None, an outright lookup failure included: deploying is what the
    caller would otherwise do unconditionally, so being unable to read the
    answer must cost no more than having no answer to read. No URL is returned
    that a lookup has not just confirmed.
    """
    marker_function = get_source_marker_function_name(function)
    try:
        if not modal_interface.is_function_deployed(
            marker_function, app_name=app_name, environment_name=environment_name
        ):
            return None
        url = get_function_url(function, app_name, environment_name, modal_interface)
    except (ModalProxyError, MngrError) as e:
        logger.debug("Could not tell whether app {} already carries {} ({}) -- deploying", app_name, function, e)
        return None
    logger.trace("App {} already carries the current {} function", app_name, function)
    return url


def ensure_function_deployed(
    function: str,
    app_name: str,
    environment_name: str,
    modal_interface: ModalInterface,
    *,
    retry_budget_seconds: float = DEPLOY_RETRY_BUDGET_SECONDS,
    max_backoff_seconds: float = DEPLOY_RETRY_MAX_BACKOFF_SECONDS,
) -> str:
    """Make sure an app publishes the current version of a route function, and return its URL.

    Owns the one retry over deploying: every failure Modal produced without
    judging the script -- see ``ModalProxyDeployRetryableError`` -- is
    re-attempted here under one budget.

    The endpoint belongs to the app, not to whatever is being created, so
    deploying it unconditionally takes Modal's per-app deploy lock for no
    reason, and concurrent callers then knock each other over: the losers fail
    their deploy, and a lookup racing someone else's deploy finds no function
    at all.

    The source check is therefore re-run on every attempt, not just the first.
    On an app carrying no deploy yet -- a fresh CI run's app, say -- every
    concurrent caller finds the marker missing and deploys, and Modal refuses
    all but one. A refused caller's next attempt finds the winner's marker and
    adopts its endpoint, rather than taking the lock again to publish the same
    source and making every caller still queued behind it wait for that too.
    """
    retrying = Retrying(
        retry=retry_if_exception_type(ModalProxyDeployRetryableError),
        stop=stop_after_delay(retry_budget_seconds),
        wait=wait_exponential(
            multiplier=1,
            min=min(DEPLOY_RETRY_MIN_BACKOFF_SECONDS, max_backoff_seconds),
            max=max_backoff_seconds,
        ),
        before_sleep=_log_deploy_retry,
        reraise=True,
    )
    try:
        return retrying(
            _deploy_unless_a_concurrent_deploy_got_there_first,
            function,
            app_name,
            environment_name,
            modal_interface,
        )
    except ModalProxyDeployRetryableError as e:
        raise MngrError(
            f"Failed to deploy {function} function: Modal kept failing the deploy into app "
            f"{app_name!r} for {retry_budget_seconds}s, last with: {e}"
        ) from e


def _deploy_unless_a_concurrent_deploy_got_there_first(
    function: str,
    app_name: str,
    environment_name: str,
    modal_interface: ModalInterface,
) -> str:
    """One attempt at ``ensure_function_deployed``: adopt this source if present, else deploy it."""
    url = _url_if_app_already_carries_source(function, app_name, environment_name, modal_interface)
    if url is not None:
        return url
    return deploy_function(function, app_name, environment_name, modal_interface)


def deploy_function(
    function: str,
    app_name: str,
    environment_name: str,
    modal_interface: ModalInterface,
) -> str:
    """Deploy a Function to Modal with the given app name and return the URL.

    The app is taken through the atomic get-or-create first, so a deploy is
    never what brings an app into existence: Modal's own app creation inside
    ``modal deploy`` is not atomic, and concurrent first deploys of one app
    name mint rival apps that fight over it.

    Raises MngrError if deployment fails, except when Modal failed the deploy
    in a way another attempt could clear: that propagates as a
    ``ModalProxyDeployRetryableError`` so ``ensure_function_deployed``'s retry
    can see it.
    """
    script_path = get_route_script_path(function)

    with log_span("Deploying {} function for app: {}", function, app_name):
        try:
            modal_interface.app_lookup(app_name, create_if_missing=True, environment_name=environment_name)
            modal_interface.deploy(
                script_path,
                app_name=app_name,
                environment_name=environment_name,
                extra_env={"MNGR_MODAL_SOURCE_MARKER_NAME": get_source_marker_function_name(function)},
            )
        except ModalProxyDeployRetryableError:
            # Propagate unwrapped: ensure_function_deployed's retry needs to
            # see it, so it can re-check whether a deploy that beat us to this
            # app published this very source -- including one of our own
            # attempts that Modal cut off after it had landed.
            raise
        except ModalProxyError as e:
            raise MngrError(f"Failed to deploy {function} function: {e}") from e

    try:
        return _get_function_url_after_deploy(function, app_name, environment_name, modal_interface)
    except ModalProxyNotFoundError as e:
        # The retry below needs to see the raw not-found type, but at this
        # boundary we keep the documented MngrError contract for callers.
        raise MngrError(f"Failed to look up deployed {function} function after deploy: {e}") from e


# Modal's control plane is not immediately read-consistent after a deploy: a
# function lookup right after `deploy` returns can transiently answer
# not-found, and concurrent deploys of the same app widen that window. Since
# the deploy just succeeded, a not-found here is transient by construction, so
# retry briefly before giving up. Lookups that did NOT just deploy (bare
# get_function_url) stay fail-fast.
@retry(
    retry=retry_if_exception_type(ModalProxyNotFoundError),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def _get_function_url_after_deploy(
    function: str,
    app_name: str,
    environment_name: str,
    modal_interface: ModalInterface,
) -> str:
    return get_function_url(function, app_name, environment_name, modal_interface)


def get_function_url(
    function: str,
    app_name: str,
    environment_name: str,
    modal_interface: ModalInterface,
) -> str:
    """Look up the web URL for an already-deployed Modal function.

    Raises ModalProxyNotFoundError when the function is not (yet) visible, and
    MngrError for any other lookup failure or a function with no web URL.
    """
    with log_span("Looking up URL for deployed {} function in app: {}", function, app_name):
        try:
            func = modal_interface.function_from_name(
                name=function,
                app_name=app_name,
                environment_name=environment_name,
            )
        except ModalProxyNotFoundError:
            # Propagate not-found unwrapped: the post-deploy retry above needs
            # to see it, and the other raise point in this function
            # (get_web_url below) already propagates it unwrapped.
            raise
        except ModalProxyError as e:
            raise MngrError(f"Failed to look up deployed {function} function: {e}") from e

        web_url = func.get_web_url()
        if not web_url:
            raise MngrError(f"Could not find function URL for {function}")

    logger.trace("Found {} function URL: {}", function, web_url)
    return web_url
