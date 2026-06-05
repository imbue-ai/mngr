# Remove `BaseMngrError`; collapse redundant `except` clauses

## Refined prompt

> we want to *remove* BaseMngrError entirely--all of our errors should now inherit from MngrError directly.
>
> We should collapse except clauses that now redundantly / pointlessly list different types of errors (where some types are already strictly covered by another type already in the list)
>
> * Use the Concise plan template (Overview, Expected behavior, Changes)
> * Accept that all mngr errors become `ClickException` / user-facing (clean `Error:` rendering, no traceback) -- there is no internal "show-the-traceback" error tier remaining
> * Collapse rule: drop a type from a multi-type `except` clause only when it is a strict subclass of another type already in that same clause (verified across packages); leave unrelated types like `OSError`, `docker.errors.*`, `BaseExceptionGroup`, and `ModalProxyError` (which is not a `MngrError`) intact
> * Delete the tests that exist to document the old two-tier hierarchy (`mngr/errors_test.py::test_consolidated_errors_are_mngr_errors`, `mngr_robinhood/errors_test.py::test_robinhood_errors_are_mngr_errors`) outright; scrub any live docs of the distinction (only changelog files still mention `BaseMngrError`, and those stay as historical records)
> * In `imbue_common/logging_test.py`, replace the `BaseMngrError` usage with a local test-only `Exception` subclass, removing the `imbue_common` -> `imbue.mngr` cross-import
> * Special case (`main.py` top-level handler): keep an explicit `MngrError` in the tuple for readability even though it is now covered by `click.ClickException`

## Overview

- `errors.py` currently defines two base classes: `BaseMngrError(Exception)` and `MngrError(ClickException, BaseMngrError)`. The only behavioral difference is that `MngrError` is also a `click.ClickException` (renders as a clean `Error: ...` line instead of a traceback).
- Prior commits already moved **every** mngr error class to inherit from `MngrError` (or a `MngrError` subclass). `BaseMngrError` now has exactly one real subclass -- `MngrError` itself -- plus one test sentinel.
- This change finalizes that consolidation: delete `BaseMngrError`, make `MngrError` inherit directly from `click.ClickException`, and repoint every remaining `BaseMngrError` reference to `MngrError`.
- Because `BaseMngrError` was a strict superclass of `MngrError` and nothing else now subclasses it directly, every `except BaseMngrError` catches exactly the same set of exceptions as `except MngrError`. The replacement is behavior-preserving.
- Opportunistically collapse `except` tuples where one mngr error type is now strictly covered by another mngr error type in the same tuple (e.g. `(VpsApiError, MngrError)` -> `MngrError`, `(MngrError, UserInputError)` -> `MngrError`).

## Expected behavior

- No user-visible behavior change. All mngr errors continue to render as clean `Error: ...` messages (with any `user_help_text`) at the CLI, exactly as they do today.
- `from imbue.mngr.errors import BaseMngrError` no longer resolves; any new code must import `MngrError`.
- Every exception type defined across `mngr` and its plugins remains a `MngrError` (and therefore a `click.ClickException`); there is no error tier that surfaces as a raw traceback to the user by design.
- Catch behavior is identical before and after: each rewritten `except` clause catches the same set of exception types it caught previously.
- `imbue_common` tests no longer import from `imbue.mngr` (one fewer lower-layer-reaches-upward dependency in the test suite).

## Changes

### `libs/mngr` -- core error definition

- `imbue/mngr/errors.py`:
  - Delete `class BaseMngrError(Exception)`.
  - Change `class MngrError(ClickException, BaseMngrError)` -> `class MngrError(ClickException)`.
  - Reword the `HostError` and `AgentError` docstrings that say "Inherits from MngrError (not just BaseMngrError) so that ..." to drop the obsolete `BaseMngrError` contrast (state simply that they are `MngrError` subclasses and thus user-facing `ClickException`s).

### `libs/mngr` -- repoint `BaseMngrError` references to `MngrError`

For each, update the import (`BaseMngrError` -> `MngrError`, removing a now-duplicate `MngrError` import if one already exists) and the usage:

- `imbue/mngr/main.py:115` -- `except (click.ClickException, click.Abort, click.exceptions.Exit, BaseMngrError, bdb.BdbQuit)` -> replace `BaseMngrError` with `MngrError` and **keep it** (per decision: explicit for readability, even though `MngrError` is covered by `click.ClickException`).
- `imbue/mngr/hosts/host.py:612` -- `except (BaseMngrError, OSError)` -> `except (MngrError, OSError)`.
- `imbue/mngr/api/observe.py:432,570,618` -- `except (BaseMngrError, OSError)` -> `except (MngrError, OSError)`.
- `imbue/mngr/api/message.py:283` -- `except BaseMngrError` -> `except MngrError`; update the docstring at line 255 ("Known errors (BaseMngrError) ...") to say `MngrError`.
- `imbue/mngr/api/discovery_events.py:516` -- `except (BaseMngrError, OSError, ValueError)` -> `except (MngrError, OSError, ValueError)` (all three retained; unrelated types).
- `imbue/mngr/cli/snapshot.py:132,507` -- `except BaseMngrError` -> `except MngrError`.
- `imbue/mngr/cli/headless_runner.py:85,101,105` -- `except (OSError, BaseMngrError)` -> `except (OSError, MngrError)`.

### `libs/mngr` -- collapse redundant mngr-type tuples

- `imbue/mngr/api/exec.py:205` -- `except (MngrError, UserInputError)` -> `except MngrError` (`UserInputError` is a `MngrError` subclass).
- `imbue/mngr/api/list.py:728` -- `isinstance(e, (MngrError, BaseMngrError))` -> `isinstance(e, MngrError)`; remove the `BaseMngrError` import.

### `libs/mngr` -- tests / fixtures

- `imbue/mngr/conftest.py:811` -- `class _DockerdStartupError(BaseMngrError)` -> `class _DockerdStartupError(MngrError)`; update the import at line 34.
- `imbue/mngr/errors_test.py` -- delete `test_consolidated_errors_are_mngr_errors` (and its `@pytest.mark.parametrize` block); remove any imports that become unused as a result. (See note in "Risks / flags".)
- `imbue/mngr/api/message_test.py:273` -- update the docstring that references `except BaseMngrError` to `except MngrError`.

### `libs/imbue_common`

- `imbue/imbue_common/logging_test.py` -- remove `from imbue.mngr.errors import BaseMngrError`; define a local test-only `class _SampleLoggingError(Exception)` in the test module and use it in the two `raise`/`except` pairs (lines ~168-169, ~407-408). This removes the `imbue_common` -> `imbue.mngr` cross-import.

### `libs/mngr_robinhood`

- `imbue/mngr_robinhood/cli.py:71` -- `except BaseMngrError` -> `except MngrError`; update import.
- `imbue/mngr_robinhood/orchestrator.py` -- `:298,376` `except BaseMngrError` -> `except MngrError`; `:698,702` `except (OSError, BaseMngrError)` -> `except (OSError, MngrError)`; update import; reword the `BaseMngrError` comments at lines ~233 and ~270 to reference `MngrError`.
- `imbue/mngr_robinhood/errors_test.py` -- delete `test_robinhood_errors_are_mngr_errors`; remove imports that become unused.

### `libs/mngr_tutor`

- `imbue/mngr_tutor/checks.py:93` -- `except (BaseMngrError, OSError)` -> `except (MngrError, OSError)`; update import at line 8.

### `libs/mngr_lima`

- `imbue/mngr_lima/instance.py:488` -- `except (LimaCommandError, MngrError, OSError)` -> `except (MngrError, OSError)` (`LimaCommandError` is a `MngrError` subclass).

### `libs/mngr_ovh`

Collapse `VpsApiError` / `VpsProvisioningError` (both subclasses of `VpsDockerError`, which subclasses `MngrError`) where they co-occur with `MngrError`:

- `imbue/mngr_ovh/recycle.py:166,215,332,341,401` -- `except (VpsApiError, MngrError)` -> `except MngrError`.
- `imbue/mngr_ovh/backend.py:144,202,222,336,590` -- `except (VpsApiError, MngrError)` -> `except MngrError`.
- `imbue/mngr_ovh/cli.py:91,117` -- `except (VpsApiError, MngrError)` -> `except MngrError`.
- `imbue/mngr_ovh/ordering.py:185` -- `except (MngrError, VpsApiError, VpsProvisioningError)` -> `except MngrError`.
- `imbue/mngr_ovh/ordering.py:233` -- `except (VpsApiError, MngrError)` -> `except MngrError`.
- Remove `VpsApiError` / `VpsProvisioningError` imports in each file if they become unused after collapsing.

### Explicitly NOT changed (out of scope per the collapse rule)

- Clauses pairing `MngrError` with unrelated types stay as-is: `(MngrError, OSError)`, `(MngrError, FileNotFoundError)`, `(MngrError, OSError, BaseExceptionGroup)`, `(MngrError, docker.errors.DockerException, ...)`, `(KeyError, ValueError, MngrError)`, `(ModalProxyError, MngrError)` (`ModalProxyError` is a plain `Exception`, not a `MngrError`).
- Pre-existing non-mngr redundancies (e.g. `(FileNotFoundError, OSError, MngrError)` in `providers/docker/instance.py:1427`, where `FileNotFoundError` is already covered by `OSError`) are left alone -- they are unrelated to the `BaseMngrError` removal.
- Changelog / `UNABRIDGED_CHANGELOG.md` files that mention `BaseMngrError` are historical records and are not rewritten.
- Live docs (`docs/concepts/plugins.md`, `future_specs/*.md`) reference only `MngrError` / `PluginMngrError`, never `BaseMngrError`, so they need no edits.

### Changelog entries (required by CI)

Add one per touched project at `<project_dir>/changelog/mngr-fix-error-hierarchy-collapse.md`:

- `libs/mngr/changelog/`, `libs/imbue_common/changelog/`, `libs/mngr_robinhood/changelog/`, `libs/mngr_tutor/changelog/`, `libs/mngr_lima/changelog/`, `libs/mngr_ovh/changelog/`.
- Each briefly notes: `BaseMngrError` removed; all errors now inherit from `MngrError` directly; redundant `except` clauses collapsed.

## Verification

- `just test-offload` (full suite) must pass; report the exact command and pass/fail counts.
- `grep -rn "BaseMngrError" libs/ apps/ --include="*.py"` must return zero matches after the change.
- Run `/autofix` and `/verify-conversation` per CLAUDE.md before finishing; open a draft PR.

## Risks / flags

- **Deleting the two "hierarchy" tests removes live coverage of a still-valid invariant.** `test_consolidated_errors_are_mngr_errors` and `test_robinhood_errors_are_mngr_errors` assert that the listed error types are `MngrError` / `click.ClickException` subclasses; only their docstrings mention `BaseMngrError`. Deleting them (per the chosen option) drops that assertion entirely. Alternative worth reconsidering: keep them with docstrings rewritten to drop the historical `BaseMngrError` framing.
- `MngrError` losing the `BaseMngrError` base is safe **only** because no class still inherits from `BaseMngrError` directly. The verification grep above guards against a stray subclass being missed.
