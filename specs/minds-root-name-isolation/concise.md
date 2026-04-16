# Minds root-name isolation

## Overview

- Introduce a single env var `MINDS_ROOT_NAME` (default `minds`; dev copy exports `devminds`) that drives all per-install paths and prefixes, so an installed minds and a dev minds can coexist without sharing state.
- `MINDS_ROOT_NAME=<X>` produces: minds data dir `~/.<X>`, `MNGR_HOST_DIR=~/.<X>/mngr`, `MNGR_PREFIX=<X>-`. `MNGR_ROOT_NAME` stays `mngr` so agent template repos keep their project config at `.mngr/settings.toml`.
- Translation happens in two places: the Electron shell (`backend.js`) for packaged/dev launches, and a new `apply_bootstrap()` (stdlib + loguru for the invalid-input error path) called at the top of the `minds` CLI entrypoint for standalone launches. Both mutate the environment before any `imbue.mngr.*` module loads (mngr reads `MNGR_HOST_DIR`/`MNGR_PREFIX` at import time).
- Promote two server URLs (`CLOUDFLARE_FORWARDING_URL`, `SUPERTOKENS_CONNECTION_URI`) from env-var-only into a new `MindsConfig` model loaded from `~/.<MINDS_ROOT_NAME>/config.toml`, with sensible dev-server defaults baked into code. Precedence: env > file > default. Everything else (OAuth creds, API keys, forwarding secret) stays env-var-only.
- No migration path — existing `~/.minds/` and `~/.mngr/` data is abandoned. Minds is pre-production; simplicity trumps backwards compatibility.

## Expected Behavior

**Running installed minds (no env vars set):**

- Minds uses `~/.minds/` as its data dir, creates agents under `~/.minds/mngr/`, uses tmux/env prefix `minds-`.
- Cloudflare forwarding and SuperTokens work out of the box against the current dev-deployed servers (no `.test_env` needed).
- `minds forward` logs the resolved `MINDS_ROOT_NAME`, `MNGR_HOST_DIR`, `MNGR_PREFIX`, and `MindsConfig` values at INFO on startup.

**Running dev minds (`MINDS_ROOT_NAME=devminds` exported):**

- Minds uses `~/.devminds/` as its data dir, creates agents under `~/.devminds/mngr/`, uses prefix `devminds-`.
- Dev and installed minds have fully separate Python venvs, uv caches, logs, auth state, and agent state — changing one cannot corrupt the other.
- Both versions hit the same dev-deployed Cloudflare/SuperTokens servers until production equivalents exist.

**Running standalone `mngr` (anywhere):**

- `mngr` ignores `MINDS_ROOT_NAME` entirely — only reads `MNGR_*` vars. A user's shell that exports `MINDS_ROOT_NAME=devminds` does not affect standalone `mngr` invocations.
- Subprocess `mngr` calls spawned by minds inherit the translated `MNGR_HOST_DIR`/`MNGR_PREFIX` via `os.environ` and therefore see only minds-scoped agents.

**Config resolution:**

- `MindsConfig` is loaded once at `minds forward` startup from `~/.<MINDS_ROOT_NAME>/config.toml` (flat top-level keys: `cloudflare_forwarding_url`, `supertokens_connection_uri`).
- Missing file → silently use hardcoded defaults.
- Env vars (`CLOUDFLARE_FORWARDING_URL`, `SUPERTOKENS_CONNECTION_URI`) override any value from the file.
- The resolved `MindsConfig` is threaded explicitly from `forward.py` through `start_desktop_client` into `_build_cloudflare_client`/`_init_supertokens`; `runner.py` no longer reads those env vars.

**Invalid input:**

- `MINDS_ROOT_NAME` is validated against `[a-z0-9_-]+` by the bootstrap. Invalid values exit with stderr message and status 1 before any other code runs.
- URL fields are validated as pydantic `AnyUrl` on model load; malformed URLs raise at startup.

## Changes

**New files:**

- `apps/minds/imbue/minds/bootstrap.py` — minimal module (stdlib + loguru only; loguru is used for the invalid-input error path and does not transitively import mngr, so the "runs before mngr is imported" guarantee is preserved) exposing `apply_bootstrap()`. Reads `MINDS_ROOT_NAME` (default `minds`), regex-validates it (`re.fullmatch(r"[a-z0-9_-]+", ...)`), then sets `MNGR_HOST_DIR=~/.<X>/mngr` and `MNGR_PREFIX=<X>-` in `os.environ`. Shape mirrors `libs/mngr/imbue/mngr/config/host_dir.py`.
- `apps/minds/imbue/minds/cli_entry.py` — new home for the click CLI group currently in `main.py` (moved so `main.py` can stay a tiny pre-import shim).
- `apps/minds/imbue/minds/config/loader.py` — `load_minds_config(data_dir: Path) -> MindsConfig` that reads `data_dir/config.toml` if present, overlays env-var values for each field using the field's pydantic `alias`, and validates.
- `apps/minds/imbue/minds/bootstrap_test.py` — unit tests for translation, default, validation, invalid-input exit.
- `apps/minds/imbue/minds/config/loader_test.py` — unit tests for precedence (env > file > default), missing file, `AnyUrl` validation.

**Changed files:**

- `apps/minds/imbue/minds/main.py` — restructured to `def main(): apply_bootstrap(); from imbue.minds.cli_entry import cli; cli()`. The package's `console_scripts` entry in `pyproject.toml` points at `imbue.minds.main:main`.
- `apps/minds/imbue/minds/primitives.py` — adds `MindsRootName(NonEmptyStr)` with `[a-z0-9_-]+` validation for typed Python use. Migrates `CloudflareForwardingUrl` from `NonEmptyStr` subclass to `AnyUrl` subclass (URL-validated; name preserved).
- `apps/minds/imbue/minds/config/data_types.py` — adds `MindsConfig(FrozenModel)` with two fields: `cloudflare_forwarding_url: AnyUrl = Field(default=..., alias="CLOUDFLARE_FORWARDING_URL")` and `supertokens_connection_uri: AnyUrl = Field(default=..., alias="SUPERTOKENS_CONNECTION_URI")`. Defaults baked in: `https://joshalbrecht--cloudflare-forwarding-fastapi-app.modal.run` and `https://st-dev-aba73a80-3754-11f1-9afe-f5bb4fa720bc.aws.supertokens.io`. `WorkspacePaths` gains a way to expose the mngr host dir (`data_dir / "mngr"`) for explicit threading.
- `apps/minds/imbue/minds/cli/forward.py` — removes the `--data-dir` CLI flag. Derives `data_directory` from the bootstrap-set `MINDS_ROOT_NAME` (or reads it directly). Loads `MindsConfig` via `load_minds_config(data_directory)`. Passes `mngr_host_dir` and `minds_config` explicitly into `start_desktop_client`. Emits the startup INFO log with resolved values.
- `apps/minds/imbue/minds/desktop_client/runner.py` — removes `_DEFAULT_MNGR_HOST_DIR` constant and the env-var reads inside `_build_cloudflare_client`/`_init_supertokens`. Accepts `mngr_host_dir: Path` and `minds_config: MindsConfig` as parameters; threads them to `AgentDiscoveryHandler` and the init helpers.
- `apps/minds/imbue/minds/desktop_client/api_v1.py` — reads `cloudflare_forwarding_url` from `app.state.minds_config` instead of `os.environ.get("CLOUDFLARE_FORWARDING_URL")`.
- `apps/minds/imbue/minds/desktop_client/cloudflare_client.py` — `CloudflareForwardingUrl` type changes propagate (now `AnyUrl`-based); constructors and tests adjust.
- `apps/minds/electron/backend.js` — reads `process.env.MINDS_ROOT_NAME` (default `minds`), computes `MNGR_HOST_DIR`/`MNGR_PREFIX`, adds them to the child env passed to the spawned `minds forward` process (both dev and packaged branches).
- `apps/minds/electron/paths.js` — `getDataDir()` returns `path.join(os.homedir(), '.' + (process.env.MINDS_ROOT_NAME || 'minds'))`, which cascades through `getUvCacheDir`, `getUvPythonDir`, `getLogDir`, `getVenvDir` so dev and installed electron launches use fully separate venvs/caches.
- `apps/minds/docs/desktop-app.md` — updates the "Data directory" section to describe `MINDS_ROOT_NAME`, the `~/.<MINDS_ROOT_NAME>/mngr/` layout, and the new `config.toml` location.
- `apps/minds/docs/design.md` — updates the Cloudflare/SuperTokens configuration description to reference `MindsConfig`, its defaults, and env-var overrides.

**Removed files / code:**

- `.test_env` — deleted (its `CLOUDFLARE_FORWARDING_URL` becomes the built-in default; `OWNER_EMAIL` is unused).
- `DEFAULT_DATA_DIR_NAME` and `get_default_data_dir()` in `config/data_types.py` — removed; data dir is derived from `MINDS_ROOT_NAME` in one place.
- `_DEFAULT_MNGR_HOST_DIR` in `runner.py` — removed; the value is passed in explicitly.
- `--data-dir` option on `minds forward` — removed.

**Test surface:**

- `apps/minds/imbue/minds/cli/conftest.py` continues to set `MNGR_HOST_DIR`/`MNGR_PREFIX`/`MNGR_ROOT_NAME` directly per test (bypasses bootstrap, matches today's pattern).
- New colocated unit tests (`bootstrap_test.py`, `config/loader_test.py`) cover the new translation and config-loader logic directly.
- Existing tests that asserted on `_DEFAULT_MNGR_HOST_DIR` (`runner_test.py`) are updated to assert on the explicitly-passed `mngr_host_dir` parameter instead.
- Existing tests that set `CLOUDFLARE_FORWARDING_URL`/`SUPERTOKENS_CONNECTION_URI` as env vars continue to work unchanged (env still overrides config).
