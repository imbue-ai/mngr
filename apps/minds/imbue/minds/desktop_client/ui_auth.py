"""The /ui session check, in a leaf module every /ui area can import.

``ui_api`` imports each area module to register its routes, so an area module
importing the check back from ``ui_api`` would be circular; this module imports
nothing from the /ui surface.
"""

import os

from flask import request

from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import verify_session_cookie
from imbue.minds.desktop_client.state import get_state


def is_ui_request_authenticated() -> bool:
    """Whether the current request carries a valid global session cookie.

    Mirrors the legacy chrome's check (including the ``SKIP_AUTH`` test/dev
    escape hatch) so the SPA and the remaining legacy routes always agree on
    who is signed in.
    """
    if os.getenv("SKIP_AUTH", "0") == "1":
        return True
    signing_key = get_state().auth_store.get_signing_key()
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value is None:
        return False
    return verify_session_cookie(cookie_value=cookie_value, signing_key=signing_key)
