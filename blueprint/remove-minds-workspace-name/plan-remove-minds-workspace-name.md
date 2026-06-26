# Remove the `MINDS_WORKSPACE_NAME` env var

## Overview

- `MINDS_WORKSPACE_NAME` is a dev-only operator override that seeds the create-form's default *host/workspace name*, honored only under the `MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1` opt-in. It no longer reaches the agent (already dropped from FCT's `[commands.create].pass_env`); it only influences the workspace name.
- Validation confirmed it is the weakest of the three `MINDS_WORKSPACE_*` siblings: the e2e runner sets it but then types the name into `#host_name` explicitly (`_ensure_field_value` overrides any prefill), and the default `just minds-start` leaves it unset so the form auto-generates `mind-N`. Its only live value is the CLI convenience of pinning a predictable name before the form opens.
- That convenience is fully recoverable without the var: the create-form's advanced "Name" field (`#host_name`, the submitted-name path) lets the operator type a predictable name, which is what the dev-loop recipes (`just propagate-changes <name>`, `just forward-system-interface <name>`) target. So removing the var loses no capability.
- This completes the `gleb-onboarding-shorter` direction (which already de-defaulted the name to "let the form pick `mind-N`"): the override path is now removed entirely rather than left dormant.
- The sibling `MINDS_WORKSPACE_GIT_URL` / `MINDS_WORKSPACE_BRANCH` prefills and the `MINDS_USE_LOCAL_WORKSPACE_DEFAULTS` opt-in are genuinely load-bearing for local dev iteration and are kept untouched.

## Expected behavior

- Creating a workspace resolves its name from exactly two sources: the name typed into the form's advanced "Name" field (used verbatim), else the next free auto-generated `mind-N`. The operator-override step in between is gone.
- A stray `MINDS_WORKSPACE_NAME` left in an operator's shell has no effect on any tier, under any opt-in state — silently ignored, no warning. (Previously it was honored under the opt-in.)
- `just minds-start` no longer accepts an agent-name positional; it always launches with the name unset, so the form auto-generates `mind-N` unless the operator types a name. `branch` / `fct` become named-only arguments (`just minds-start branch=my-branch fct=...`) to avoid positional ambiguity.
- The desktop client's create-form prefill still auto-fills repository and branch from `MINDS_WORKSPACE_GIT_URL` / `_BRANCH` (under the opt-in), but no longer prefills the name — matching what a shipped end-user binary does.
- The e2e acceptance test (`test_desktop_client.py` via `e2e_workspace_runner`) is unaffected: it already types the workspace name into the form and destroys the agent by that typed name; only the redundant env-var prefill is removed.
- Existing in-flight create flows, the `mind-N` auto-numbering, and collision handling (a typed/auto name that collides errors at create time rather than being silently renamed) are unchanged.

## Changes

- **`apps/minds/imbue/minds/desktop_client/templates.py`** — drop the `MINDS_WORKSPACE_NAME` operator-override branch from `resolve_create_host_name`, reducing its resolution order from three steps to two (submitted name → generated `mind-N`); update the function docstring accordingly. Scrub `_NAME` mentions from `_operator_workspace_default`'s docstring/comments while leaving the helper itself in place (still used by GIT_URL / BRANCH).
- **`apps/minds/imbue/minds/desktop_client/app.py`** — update the create-handler comment that describes the now-removed operator-override step in the name-resolution order.
- **`apps/minds/imbue/minds/desktop_client/e2e_workspace_runner.py`** — remove the `env["MINDS_WORKSPACE_NAME"] = workspace_name` line from `_build_electron_env`, and drop its now-unused `workspace_name` parameter (and the corresponding argument at the call site); update the docstring/comments referencing the name prefill.
- **`apps/minds/imbue/minds/desktop_client/templates_test.py`** — remove the three operator-override tests (`..._honors_operator_override_when_opted_in`, `..._operator_override_is_not_uniquified`, `..._ignores_operator_override_without_opt_in`) and the now-unnecessary `monkeypatch.delenv("MINDS_WORKSPACE_NAME", ...)` lines in the remaining name-resolution tests. The `mind-N` fallback and submitted-name tests stay.
- **`justfile`** — remove the `agent_name` parameter from the `minds-start` recipe and the `MINDS_WORKSPACE_NAME` export/unset block; convert `branch` / `fct` to named-only arguments. Update the recipe's doc-comment block (the `MINDS_WORKSPACE_*` description and usage examples) to drop name-pinning references.
- **`.claude/skills/minds-dev-workflow/SKILL.md`** — remove the `MINDS_WORKSPACE_NAME` env-var table row and the commented example; drop the "name" mention from the create-form auto-fill descriptions (the "What `just minds-start` does" step and the prefill bullet) so they read "repository and branch". No replacement note.
- **Changelog** — add one entry per touched project: `apps/minds/changelog/remove-minds-workspace-env-var.md` (the create-form / e2e behavior) and `dev/changelog/remove-minds-workspace-env-var.md` (the `just minds-start` signature change). Minimal framing: "removed the `MINDS_WORKSPACE_NAME` dev override".
- **Out of scope (unchanged):** the `MINDS_WORKSPACE_GIT_URL` / `_BRANCH` vars, the `MINDS_USE_LOCAL_WORKSPACE_DEFAULTS` opt-in, FCT's `pass_env` (already free of the var), and the historical `specs/` files that reference the var (left as point-in-time records).
