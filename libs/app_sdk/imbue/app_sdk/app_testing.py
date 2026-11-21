"""What an app's own tests assert about the document it serves (``imbue.app_sdk.api_schema``) and about the request
guard ahead of its routes (``imbue.app_sdk.request_guard``).

Needs the ``testing`` extra, which carries the OpenAPI validator.
"""

import inspect
import tomllib
from pathlib import Path
from typing import Any
from typing import Final

from flask.testing import FlaskClient
from openapi_spec_validator import validate
from pydantic import ConfigDict
from pydantic import Field

from imbue.app_sdk.api_schema import API_SCHEMA_ENDPOINT
from imbue.app_sdk.api_schema import EXTENSION_KEY
from imbue.app_sdk.api_schema import OTHER_METHODS_KEY
from imbue.app_sdk.api_schema import RouteModels
from imbue.app_sdk.api_schema import RouteTable
from imbue.app_sdk.api_schema import flask_rule_to_openapi_path
from imbue.app_sdk.api_schema import is_static_endpoint
from imbue.app_sdk.frozen_model import FrozenModel
from imbue.app_sdk.identity import FETCH_MODE_HEADER
from imbue.app_sdk.identity import FETCH_SITE_HEADER
from imbue.app_sdk.identity import IDENTITY_HEADER
from imbue.app_sdk.identity import OTHER_ORIGIN_REFUSAL
from imbue.app_sdk.identity import UNTRUSTED_LOOPBACK_REFUSAL

# A view that reads its body through Flask directly bypasses ``parse_request_body`` and so the route table.
_RAW_BODY_READS: Final[tuple[str, ...]] = ("request.get_json(", "request.json", ".get_json(")

# The fetch metadata a browser sends for a page of another origin's fetch, and the header a proxy stamps on the
# owner's request.
CROSS_SITE_FETCH_HEADERS: Final[dict[str, str]] = {
    FETCH_SITE_HEADER: "cross-site",
    FETCH_MODE_HEADER: "cors",
}
OWNER_IDENTITY_HEADERS: Final[dict[str, str]] = {IDENTITY_HEADER: '{"owner":true}'}
# A Host that is not loopback: the one a page reaching loopback through a rebound name sends, and the one a proxy
# keeps on a request it stamped.
REBOUND_BASE_URL: Final[str] = "http://rebound.example:8090"
FORWARDED_BASE_URL: Final[str] = "http://app-x.agent-1.localhost:8421"


class _DescribedManifest(FrozenModel):
    """The three fields of an app's manifest its API document repeats."""

    # A manifest carries many more tables than the three fields the document repeats.
    model_config = ConfigDict(extra="ignore")

    display_name: str = Field(description="What users see, and the document's title")
    description: str | None = Field(default=None, description="What the app is for, and the document's description")
    api_schema_path: str | None = Field(default=None, description="Where the app answers its API document")


def _is_schema_endpoint(endpoint: str) -> bool:
    return endpoint.rsplit(".", 1)[-1] == API_SCHEMA_ENDPOINT


def _views_reading_their_body_raw(client: FlaskClient, route_table: RouteTable) -> list[str]:
    """The routes of the table's ``RouteModels`` entries whose view reads the request body without the helper."""
    offenders: list[str] = []
    application = client.application
    for rule in application.url_map.iter_rules():
        if is_static_endpoint(rule.endpoint) or _is_schema_endpoint(rule.endpoint):
            continue
        view = application.view_functions.get(rule.endpoint)
        if view is None:
            continue
        openapi_path = flask_rule_to_openapi_path(rule.rule)
        entries = [route_table.get((method, openapi_path)) for method in rule.methods or frozenset()]
        is_modelled = any(isinstance(entry, RouteModels) and not entry.is_body_parsed_by_view for entry in entries)
        if not is_modelled:
            continue
        try:
            source = inspect.getsource(inspect.unwrap(view))
        except (OSError, TypeError):
            continue
        if any(marker in source for marker in _RAW_BODY_READS):
            offenders.append(f"{rule.endpoint} ({openapi_path})")
    return sorted(set(offenders))


def assert_request_guard_refuses_unknown_provenance(
    client: FlaskClient, method: str, path: str, **request_kwargs: Any
) -> None:
    """The request guard (``imbue.app_sdk.request_guard``) is ahead of ``path``: the request is refused with 403
    when a browser marks it as a page of another origin's and when it is header-less under a Host that is not
    loopback, both before its view runs, and reaches its view when a proxy stamped it, whatever Host the proxy kept.

    ``request_kwargs`` go to every probe (a JSON body, say). The proxied probe runs the view, so name a request the
    app answers freely.
    """
    cross_site = client.open(path, method=method, headers=CROSS_SITE_FETCH_HEADERS, **request_kwargs)
    assert cross_site.status_code == 403, cross_site.data
    assert cross_site.get_json() == {"detail": OTHER_ORIGIN_REFUSAL}
    rebound = client.open(path, method=method, base_url=REBOUND_BASE_URL, **request_kwargs)
    assert rebound.status_code == 403, rebound.data
    assert rebound.get_json() == {"detail": UNTRUSTED_LOOPBACK_REFUSAL}
    proxied = client.open(
        path,
        method=method,
        headers=OWNER_IDENTITY_HEADERS,
        base_url=FORWARDED_BASE_URL,
        **request_kwargs,
    )
    assert proxied.status_code == 200, proxied.data


def assert_api_schema_describes_app(client: FlaskClient, manifest_path: Path) -> None:
    """The app answers its manifest's ``api_schema_path`` with a valid document that describes every route.

    The document's title and description are the manifest's ``display_name``
    and ``description``; every rule of the app's url map except Flask's static
    routes and the schema route itself appears among its paths; every operation
    carries a summary (the first paragraph of its view's docstring); every
    operation is in the app's route table, with its models or the reason it
    exchanges no JSON, and the table names no route the app lacks; and no
    modelled route reads its body around ``parse_request_body`` unless its entry
    says the view parses the body itself.
    """
    manifest = _DescribedManifest.model_validate(tomllib.loads(manifest_path.read_text(encoding="utf-8")))
    assert manifest.api_schema_path is not None, f"{manifest_path} declares no api_schema_path"
    assert manifest.description is not None, f"{manifest_path} declares no description"
    response = client.get(manifest.api_schema_path)
    assert response.status_code == 200, response.data
    document = response.get_json()
    validate(document)
    assert document["info"]["title"] == manifest.display_name
    assert document["info"]["description"] == manifest.description
    expected_paths = {
        flask_rule_to_openapi_path(rule.rule)
        for rule in client.application.url_map.iter_rules()
        if not is_static_endpoint(rule.endpoint) and not _is_schema_endpoint(rule.endpoint)
    }
    assert set(document["paths"]) == expected_paths
    operations = {
        (method.upper(), path): operation
        for path, path_item in document["paths"].items()
        for method, operation in path_item.items()
        if method != OTHER_METHODS_KEY
    }
    undescribed = [
        f"{method} {path}" for (method, path), operation in operations.items() if "summary" not in operation
    ]
    assert not undescribed, "these routes have no docstring, so the document says nothing about them: " + ", ".join(
        undescribed
    )
    route_table: RouteTable | None = client.application.extensions.get(EXTENSION_KEY)
    assert route_table is not None, "the app registered no route table: pass route_table to register_api_schema_route"
    untabled = [f"{method} {path}" for (method, path) in operations if (method, path) not in route_table]
    assert not untabled, (
        "these routes are not in the route table (name their models, or declare them schemaless): "
        + ", ".join(untabled)
    )
    stale = [f"{method} {path}" for (method, path) in route_table if (method, path) not in operations]
    assert not stale, "the route table names routes the app does not have: " + ", ".join(stale)
    raw_readers = _views_reading_their_body_raw(client, route_table)
    assert not raw_readers, (
        "these views read the request body without parse_request_body, so the table cannot vouch for them: "
        + ", ".join(raw_readers)
    )
