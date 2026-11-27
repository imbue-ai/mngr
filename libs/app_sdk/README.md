# imbue-app-sdk

What an [Imbue Studio](https://github.com/imbue-ai/mngr/tree/main/apps/minds) workspace app is built on.

A workspace app is a small Flask server that the workspace shell shows as a window.
This library is the part of the workspace's contract an app needs in order to run and be tested on its own, outside any workspace: who may call it, how its routes describe themselves, and how its pages reach the shell.

```bash
pip install imbue-app-sdk
```

## What it provides

| Module | What it is for |
| --- | --- |
| `imbue.app_sdk.request_guard` | `install_request_guard(app, exempt_rules)`: refuses, ahead of every route, a request made by a page of another origin, and a request with no identity header that did not come from loopback. |
| `imbue.app_sdk.identity` | The `X-Imbue-Identity` header a proxy stamps on each request, parsed into a `RequestIdentity`, and the rule the guard applies. |
| `imbue.app_sdk.api_schema` | `register_api_schema_route(...)`: serves an OpenAPI 3.1 document built from the app's Flask routes and its route table. |
| `imbue.app_sdk.routes` | `parse_request_body`, `model_response`, and `error_response`, which check every parse and every answer against the route table, so the document cannot drift from the routes. |
| `imbue.app_sdk.shell_modules` | `register_shell_module_routes(app)`: serves the shell's two browser modules, `app_contract.js` and `context_menu.js`, from the app's own origin. |
| `imbue.app_sdk.apps` | `resolve_app_url(name)`: the URL of another app in the same workspace, found by name. |
| `imbue.app_sdk.app_testing` | Assertions for an app's own test suite. Needs the `testing` extra. |

## A minimal app

```python
from flask import Flask
from flask import Response
from pydantic import BaseModel

from imbue.app_sdk.api_schema import RouteModels
from imbue.app_sdk.api_schema import SchemalessRoute
from imbue.app_sdk.api_schema import register_api_schema_route
from imbue.app_sdk.request_guard import install_request_guard
from imbue.app_sdk.routes import model_response
from imbue.app_sdk.shell_modules import SHELL_MODULE_ROUTE_KEY
from imbue.app_sdk.shell_modules import SHELL_MODULE_RULE
from imbue.app_sdk.shell_modules import register_shell_module_routes


class HealthResponse(BaseModel):
    status: str


app = Flask("notes", static_folder=None)
install_request_guard(app, exempt_rules=frozenset({SHELL_MODULE_RULE}))
register_api_schema_route(
    app,
    "/api/schema",
    title="Notes",
    description="Keep and search short notes",
    route_table={
        ("GET", "/health"): RouteModels(response_model=HealthResponse),
        SHELL_MODULE_ROUTE_KEY: SchemalessRoute(reason="a browser-side module the shell builds"),
    },
)
register_shell_module_routes(app)


@app.get("/health")
def health() -> Response:
    """Whether the app is up."""
    return model_response(HealthResponse(status="ok"))
```

## Running outside a workspace

Nothing here needs a workspace to import or to run.
The shell is found at `MINDS_WORKSPACE_SERVER_URL` (default `http://127.0.0.1:8000`).
When no shell answers, the two browser modules are a 404 and the app's pages run without the shell's features, and `resolve_app_url` raises `ShellUnreachableError`.

## Testing an app

Install the `testing` extra in the app's development dependencies.

```python
from imbue.app_sdk.app_testing import assert_api_schema_describes_app
from imbue.app_sdk.app_testing import assert_request_guard_refuses_unknown_provenance


def test_the_app_describes_every_route(client, manifest_path) -> None:
    assert_api_schema_describes_app(client, manifest_path)


def test_the_guard_is_ahead_of_the_api(client) -> None:
    assert_request_guard_refuses_unknown_provenance(client, "GET", "/health")
```

## Dependencies and versioning

The library depends on `flask` and `pydantic` only.
It defines its own frozen model base and logs through Flask's logger, so it does not depend on `imbue-common` or `loguru`, and an app's lock does not have to follow their releases through this package.

It follows semantic versioning.
The identity header, the shell's messages, and the manifest fields it reads change only by addition.
