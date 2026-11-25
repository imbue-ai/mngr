"""The /ui session check, in a leaf module every /ui area can import.

``ui_api`` imports each area module to register its routes, so an area module
importing the check back from ``ui_api`` would be circular; this module imports
nothing from the /ui surface.
"""

import os

from flask import request

from imbue.minds.desktop_client.api_transport import is_local_trusted_environ
from imbue.minds.desktop_client.request_session import is_request_session_cookie_accepted


def is_ui_request_authenticated() -> bool:
    """Whether the current request carries a valid global session cookie, or came over the local socket.

    The one check the SPA and the remaining legacy routes share (including the
    ``SKIP_AUTH`` test/dev escape hatch), so they always agree on who is signed
    in. A request the local-socket listener accepted is the user (see
    ``api_transport``), so it needs no cookie.
    """
    if os.getenv("SKIP_AUTH", "0") == "1":
        return True
    if is_local_trusted_environ(request.environ):
        return True
    return is_request_session_cookie_accepted()
