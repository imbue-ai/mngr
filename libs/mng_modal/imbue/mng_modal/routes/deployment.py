from pathlib import Path

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mng.errors import MngError
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.interface import ModalInterface


def deploy_function(
    function: str,
    app_name: str,
    environment_name: str | None,
    modal_interface: ModalInterface,
) -> str:
    """Deploy a Function to Modal with the given app name and return the URL.

    Raises MngError if deployment fails.
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
            raise MngError(f"Failed to deploy {function} function: {e}") from e

    # get the URL out of the resulting Function object
    func = modal_interface.function_from_name(
        name=function,
        app_name=app_name,
        environment_name=environment_name,
    )
    web_url = func.get_web_url()
    if not web_url:
        raise MngError(f"Could not find function URL in deploy output for {function}")

    logger.trace("Deployed {} function, URL: {}", function, web_url)
    return web_url
