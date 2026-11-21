from flask import Flask

from imbue.app_sdk.api_schema import SchemalessRoute
from imbue.app_sdk.api_schema import register_api_schema_route
from imbue.app_sdk.app_testing import CROSS_SITE_FETCH_HEADERS
from imbue.app_sdk.conftest import ShellStandIn
from imbue.app_sdk.request_guard import install_request_guard
from imbue.app_sdk.shell_modules import SHELL_MODULE_ROUTE_KEY
from imbue.app_sdk.shell_modules import SHELL_MODULE_RULE
from imbue.app_sdk.shell_modules import register_shell_module_routes

_CONTRACT_SOURCE = b"export function connectToShell() { return 61873; }"
_MENU_SOURCE = b"export function installElementContextMenu() { return 40219; }"


def _app_serving_shell_modules() -> Flask:
    app = Flask("notes", static_folder=None)
    app.config["PROPAGATE_EXCEPTIONS"] = True
    register_shell_module_routes(app)
    return app


def test_each_module_is_served_from_the_apps_own_origin_as_the_shell_built_it(shell_stand_in: ShellStandIn) -> None:
    shell_stand_in.body_by_path["/_static/app_contract.js"] = _CONTRACT_SOURCE
    shell_stand_in.body_by_path["/_static/context_menu.js"] = _MENU_SOURCE
    client = _app_serving_shell_modules().test_client()

    contract = client.get("/_static/app_contract.js")
    menu = client.get("/_static/context_menu.js")

    assert contract.status_code == 200
    assert contract.data == _CONTRACT_SOURCE
    assert contract.mimetype == "text/javascript"
    assert menu.data == _MENU_SOURCE


def test_a_module_is_fetched_from_the_shell_once_per_process(shell_stand_in: ShellStandIn) -> None:
    shell_stand_in.body_by_path["/_static/app_contract.js"] = _CONTRACT_SOURCE
    client = _app_serving_shell_modules().test_client()

    first = client.get("/_static/app_contract.js")
    shell_stand_in.body_by_path["/_static/app_contract.js"] = b"export const changed = true;"
    second = client.get("/_static/app_contract.js")

    assert first.data == _CONTRACT_SOURCE
    assert second.data == _CONTRACT_SOURCE
    assert shell_stand_in.requested_paths == ["/_static/app_contract.js"]


def test_a_name_that_is_not_one_of_the_two_modules_never_reaches_the_shell(shell_stand_in: ShellStandIn) -> None:
    shell_stand_in.body_by_path["/_static/secrets.js"] = b"export const leaked = true;"
    client = _app_serving_shell_modules().test_client()

    assert client.get("/_static/secrets.js").status_code == 404
    assert shell_stand_in.requested_paths == []


def test_with_no_shell_to_fetch_from_a_module_is_a_404(unreachable_shell_url: str) -> None:
    client = _app_serving_shell_modules().test_client()

    assert client.get("/_static/app_contract.js").status_code == 404


def test_a_module_the_shell_could_not_give_is_fetched_again_on_the_next_request(shell_stand_in: ShellStandIn) -> None:
    client = _app_serving_shell_modules().test_client()

    missing = client.get("/_static/context_menu.js")
    shell_stand_in.body_by_path["/_static/context_menu.js"] = _MENU_SOURCE
    served = client.get("/_static/context_menu.js")

    assert missing.status_code == 404
    assert served.data == _MENU_SOURCE


def test_the_route_is_one_an_apps_table_and_guard_can_name(shell_stand_in: ShellStandIn) -> None:
    shell_stand_in.body_by_path["/_static/app_contract.js"] = _CONTRACT_SOURCE
    app = Flask("notes", static_folder=None)
    app.config["PROPAGATE_EXCEPTIONS"] = True
    install_request_guard(app, exempt_rules=frozenset({SHELL_MODULE_RULE}))
    register_api_schema_route(
        app,
        "/api/schema",
        title="Notes",
        description="Keeps notes.",
        route_table={SHELL_MODULE_ROUTE_KEY: SchemalessRoute(reason="a browser-side module the shell builds")},
    )
    register_shell_module_routes(app)
    client = app.test_client()

    imported = client.get("/_static/app_contract.js", headers=CROSS_SITE_FETCH_HEADERS)
    document = client.get("/api/schema").get_json()

    assert imported.data == _CONTRACT_SOURCE
    assert document["paths"]["/_static/{basename}"]["get"]["x-no-schema"] == "a browser-side module the shell builds"
