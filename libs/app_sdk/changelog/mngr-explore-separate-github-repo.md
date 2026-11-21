New library: `imbue-app-sdk` (`imbue.app_sdk`), what an Imbue Studio workspace app is built on. It carries the request guard and the identity header's rules, the route helpers and the OpenAPI document built from an app's routes, and test assertions for an app's own suite (`imbue.app_sdk.app_testing`, behind the `testing` extra), all moved from the default workspace template so that an app can lock and run outside a workspace.

Two functions are new: `register_shell_module_routes(app)` serves the shell's two browser modules from the app's own origin by fetching them from the running shell, and `resolve_app_url(name)` finds another app of the workspace by name.

The library depends on `flask` and `pydantic` only: it defines its own frozen model base and logs through Flask's logger, so it needs neither `imbue-common` nor `loguru`.
