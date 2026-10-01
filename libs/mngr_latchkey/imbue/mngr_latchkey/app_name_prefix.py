"""The name prefix Latchkey stamps on the things it creates on the user's behalf.

Latchkey's browser automation creates API keys, OAuth clients, developer apps
and the like inside the services it signs in to, and names each one from
``LATCHKEY_APP_NAME_PREFIX`` (upstream's default is "Latchkey"). Those names
are what the user later sees in each service's settings page, so every
Latchkey subprocess this package spawns is given the product's name instead.
"""

from typing import Final

LATCHKEY_APP_NAME_PREFIX_ENV_VAR: Final[str] = "LATCHKEY_APP_NAME_PREFIX"

APP_NAME_PREFIX: Final[str] = "Imbue"


def inject_app_name_prefix_into_env(env: dict[str, str]) -> None:
    """Set ``LATCHKEY_APP_NAME_PREFIX`` in ``env`` unless the operator already set one."""
    if env.get(LATCHKEY_APP_NAME_PREFIX_ENV_VAR):
        return
    env[LATCHKEY_APP_NAME_PREFIX_ENV_VAR] = APP_NAME_PREFIX
