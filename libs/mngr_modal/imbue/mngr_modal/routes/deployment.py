from pathlib import Path

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mngr.errors import MngrError
from imbue.modal_proxy.errors import ModalProxyError
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

    return get_function_url(function, app_name, environment_name, modal_interface)


def get_function_url(
    function: str,
    app_name: str,
    environment_name: str | None,
    modal_interface: ModalInterface,
) -> str:
    """Look up the web URL for an already-deployed Modal function.

    Raises MngrError if the function cannot be found or has no web URL.
    """
    with log_span("Looking up URL for deployed {} function in app: {}", function, app_name):
        try:
            func = modal_interface.function_from_name(
                name=function,
                app_name=app_name,
                environment_name=environment_name,
            )
        except ModalProxyError as e:
            raise MngrError(f"Failed to look up deployed {function} function: {e}") from e

        web_url = func.get_web_url()
        if not web_url:
            raise MngrError(f"Could not find function URL for {function}")

    logger.trace("Found {} function URL: {}", function, web_url)
    return web_url
