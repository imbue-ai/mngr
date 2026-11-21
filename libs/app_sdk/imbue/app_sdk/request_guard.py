"""Who may call an app's routes at all: the provenance check every workspace app installs ahead of its routes
(``imbue.app_sdk.identity``: a page of another origin is refused, and a request with no identity header is admitted
from loopback alone, under a loopback ``Host``), and the 400 for a present identity header no proxy stamped."""

from collections.abc import Set as AbstractSet
from typing import Final

from flask import Flask
from flask import Response
from flask import current_app
from flask import request
from flask.typing import ResponseReturnValue

from imbue.app_sdk.api_schema import HTTP_BAD_REQUEST
from imbue.app_sdk.errors import InvalidIdentityHeaderError
from imbue.app_sdk.identity import FETCH_MODE_HEADER
from imbue.app_sdk.identity import FETCH_SITE_HEADER
from imbue.app_sdk.identity import IDENTITY_HEADER
from imbue.app_sdk.identity import RequestProvenance
from imbue.app_sdk.identity import describe_provenance_refusal
from imbue.app_sdk.routes import error_response

HTTP_FORBIDDEN: Final[int] = 403
_EXEMPT_RULES_KEY: Final[str] = "app_sdk_request_guard_exempt_rules"


def request_provenance() -> RequestProvenance:
    """The current request's provenance, as the server saw it."""
    return RequestProvenance(
        method=request.method,
        identity_header=request.headers.get(IDENTITY_HEADER),
        fetch_site=request.headers.get(FETCH_SITE_HEADER),
        fetch_mode=request.headers.get(FETCH_MODE_HEADER),
        host=request.host,
        remote_address=request.remote_addr,
    )


def _answer_invalid_identity_header(error: InvalidIdentityHeaderError) -> Response:
    current_app.logger.warning("%s %s refused its identity header: %s", request.method, request.path, error)
    return error_response(str(error), HTTP_BAD_REQUEST)


def _refuse_requests_of_unknown_provenance() -> ResponseReturnValue | None:
    exempt_rules: frozenset[str] = current_app.extensions[_EXEMPT_RULES_KEY]
    if request.url_rule is not None and request.url_rule.rule in exempt_rules:
        return None
    provenance = request_provenance()
    refusal = describe_provenance_refusal(provenance)
    if refusal is None:
        return None
    current_app.logger.warning(
        "Refused %s %s (%s): host %r, %s %r, from %s",
        request.method,
        request.path,
        refusal,
        provenance.host,
        FETCH_SITE_HEADER,
        provenance.fetch_site,
        provenance.remote_address,
    )
    return error_response(refusal, HTTP_FORBIDDEN)


def install_request_guard(app: Flask, exempt_rules: AbstractSet[str]) -> None:
    """Refuse, ahead of every route of ``app``, a request whose provenance the check does not admit, and answer a
    present identity header that does not parse with a 400 on every route.

    Install it before any other request hook, so nothing runs for a request it refuses. ``exempt_rules`` are the
    Flask rules of routes meant for pages of other origins (a module served with a CORS header), which the check
    skips.
    """
    app.extensions[_EXEMPT_RULES_KEY] = frozenset(exempt_rules)
    app.before_request(_refuse_requests_of_unknown_provenance)
    app.register_error_handler(InvalidIdentityHeaderError, _answer_invalid_identity_header)
