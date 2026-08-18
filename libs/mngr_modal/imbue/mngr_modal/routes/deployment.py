from pathlib import Path

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


def deploy_function(
    function: str,
    app_name: str,
    environment_name: str | None,
    modal_interface: ModalInterface,
) -> str:
    """Deploy a Function to Modal with the given app name and return the URL.

    Raises MngrError if deployment fails.
    """
    script_path = Path(__file__).parent / f"{function}.py"

    with log_span("Deploying {} function for app: {}", function, app_name):
        try:
            modal_interface.deploy(
                script_path,
                app_name=app_name,
                environment_name=environment_name,
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
# not-found, and concurrent re-deploys of the same app widen that window
# (every persistent create re-deploys the app, and CI fans many creates out
# against one shared app name). Since the deploy just succeeded, a not-found
# here is transient by construction, so retry briefly before giving up.
# Lookups that did NOT just deploy (bare get_function_url) stay fail-fast.
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
