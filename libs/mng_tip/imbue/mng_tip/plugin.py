from typing import Any

from imbue.mng import hookimpl
from imbue.mng_tip.invocation_logger import log_invocation
from imbue.mng_tip.tip_display import maybe_display_tip


@hookimpl
def on_before_command(command_name: str, command_params: dict[str, Any]) -> None:
    log_invocation(command_name, command_params)
    maybe_display_tip(command_name)
