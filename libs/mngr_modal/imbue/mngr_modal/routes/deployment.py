import hashlib
from pathlib import Path
from typing import Final

from loguru import logger
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from imbue.imbue_common.logging import log_span
from imbue.mngr.errors import MngrError
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


def ensure_function_deployed(
    function: str,
    app_name: str,
    environment_name: str | None,
    modal_interface: ModalInterface,
) -> str:
    """Make sure an app publishes the current version of a route function, and return its URL.

    The endpoint belongs to the app, not to whatever is being created, so
    deploying it unconditionally takes Modal's per-app deploy lock for no
    reason, and concurrent callers then knock each other over: the losers fail
    their deploy, and a lookup racing someone else's deploy finds no function
    at all.

    Every way of failing to establish that the app already carries this exact
    source deploys, an outright lookup failure included: deploying is what the
    caller would otherwise do unconditionally, so being unable to read the
    answer must cost no more than having no answer to read. No URL is returned
    that a lookup has not just confirmed.
    """
    marker_function = get_source_marker_function_name(function)
    try:
        if modal_interface.is_function_deployed(marker_function, app_name=app_name, environment_name=environment_name):
            url = get_function_url(function, app_name, environment_name, modal_interface)
            logger.trace("App {} already carries the current {} function", app_name, function)
            return url
    except (ModalProxyError, MngrError) as e:
        logger.debug("Could not tell whether app {} already carries {} ({}) -- deploying", app_name, function, e)
    return deploy_function(function, app_name, environment_name, modal_interface)


def deploy_function(
    function: str,
    app_name: str,
    environment_name: str | None,
    modal_interface: ModalInterface,
) -> str:
    """Deploy a Function to Modal with the given app name and return the URL.

    Raises MngrError if deployment fails.
    """
    script_path = get_route_script_path(function)

    with log_span("Deploying {} function for app: {}", function, app_name):
        try:
            modal_interface.deploy(
                script_path,
                app_name=app_name,
                environment_name=environment_name,
                extra_env={"MNGR_MODAL_SOURCE_MARKER_NAME": get_source_marker_function_name(function)},
            )
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
    environment_name: str | None,
    modal_interface: ModalInterface,
) -> str:
    return get_function_url(function, app_name, environment_name, modal_interface)


def get_function_url(
    function: str,
    app_name: str,
    environment_name: str | None,
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
