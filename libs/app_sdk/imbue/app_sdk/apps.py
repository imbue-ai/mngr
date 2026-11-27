"""Finding another app of the workspace by name: nothing outside an app may hold that app's port."""

import json
from typing import Final

from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from imbue.app_sdk.errors import AppNotRegisteredError
from imbue.app_sdk.errors import ShellUnreachableError
from imbue.app_sdk.frozen_model import FrozenModel
from imbue.app_sdk.shell_client import fetch_from_shell

_INVENTORY_PATH: Final[str] = "/api/inventory"


class _InventoryApp(FrozenModel):
    """What the shell's inventory says about one app, as far as finding it goes."""

    # The inventory is the shell's document and grows; only the two fields read here are required of it.
    model_config = ConfigDict(extra="ignore")

    name: str = Field(description="The app's registered name")
    url: str = Field(description="The loopback URL the app listens on")


class _Inventory(FrozenModel):
    """The part of the shell's inventory document that lists the apps."""

    model_config = ConfigDict(extra="ignore")

    apps: tuple[_InventoryApp, ...] = Field(description="Every app the workspace has registered")


def resolve_app_url(name: str) -> str:
    """The loopback URL of the workspace app registered as ``name``, read from the shell's inventory.

    Raises AppNotRegisteredError when the workspace has no such app, and ShellUnreachableError when
    the shell does not answer or answers something that is not its inventory.
    """
    body = fetch_from_shell(_INVENTORY_PATH)
    try:
        inventory = _Inventory.model_validate(json.loads(body))
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as e:
        raise ShellUnreachableError(f"the workspace shell's inventory could not be read: {e}") from e
    for app in inventory.apps:
        if app.name == name:
            return app.url.rstrip("/")
    raise AppNotRegisteredError(f"the workspace has no app named {name!r}")
