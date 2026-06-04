Ignore local scratch shell scripts: added a general `**/*.local.sh` rule to `.gitignore` (mirroring the existing `**/*.local.md`), so any `*.local.sh` helper script stays untracked. This subsumes the previous single-file `**/scripts/notify_user.local.sh` entry, which was removed.

Also broadened the identify-* `_tasks/` ignore rule from `*/*/_tasks/` to `**/_tasks/`, so the `dev` project's root-level `dev/_tasks/` output folder is ignored consistently with the `libs/<name>/_tasks/` and `apps/<name>/_tasks/` ones (the old two-level glob missed it).

Hardened edge-case handling across `scripts/` per a suspicious-edge-case review:

- `release.py`: narrowed the broad `except Exception` in `_get_pypi_version` and `_is_published_on_pypi` to `httpx.HTTPError`, so an unexpected PyPI payload (KeyError/JSONDecodeError) now propagates instead of being silently treated as "unreachable"; the caught network error is logged.
- `josh/coordinator.py`: removed the trailing catch-all `except Exception: return set()` in `process_tasks` so unexpected errors crash loudly instead of silently corrupting deletion detection (the legitimate `except OSError` is retained).
- `modal_nuke.py`: replaced the `.get(..., "unknown")` fallback chains feeding `modal app stop`/`modal volume delete` with direct reads of the keys Modal's `--json` output actually emits (`"App ID"`, `"Name"`), raising a clear `ModalSchemaError` naming the unexpected schema if a key is missing, so the destructive path never runs against a placeholder identifier.
- `make_cli_docs.py`: dropped a dead `option.type is not None` guard, removed a redundant `hasattr(command, "commands")` guard, and made an unresolved See-Also reference raise (caught by `--check`) instead of emitting a broken markdown link.
- `sync_common_ratchets.py`: a check function in the source-of-truth file with no `# --- section ---` header now raises instead of silently syncing a bogus `# --- Unknown ---` section monorepo-wide.
- Added focused tests for `modal_nuke` and `make_cli_docs`; added clarifying comments to `junit_test_summary.py`, `warm_cli_example.py`, and the doc-inference heuristics. `warm_cli_example.py` now warns to stderr instead of silently swallowing a failed `os.chdir`.
- `make_cli_docs_test.py`: importing `make_cli_docs` sets `MNGR_LOAD_ALL_PLUGINS=1` process-wide (it must, to load all providers for doc generation); the test now pops that env var after import so the side effect cannot leak into other tests in the same xdist worker (it was breaking `libs/mngr`'s `create_plugin_manager` blocking test).
