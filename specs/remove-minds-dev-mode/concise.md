# Remove DEV mode from minds

## Overview

- DEV mode (running the agent in-place on the local machine, no Docker / SSH tunnel) is the only minds launch mode without container isolation, and is the only one that needs special-cased latchkey wiring and host-loopback dialing. It is broken in practice and is the dominant source of mode-specific branching in `agent_creator.py` and `forward_cli.py`.
- Removing DEV collapses every remaining `LaunchMode` (`LOCAL`, `LIMA`, `CLOUD`, `IMBUE_CLOUD`) onto the same shape: agent runs on a separate host accessed via SSH; latchkey is reachable at the constant agent-side port via the reverse tunnel.
- Hard removal (no deprecation): the `LaunchMode.DEV` enum value, every `case LaunchMode.DEV` branch, the DEV-only latchkey helper, the `MINDS_ALLOW_HOST_LOOPBACK` env var and the `allow_host_loopback` field on `ForwardSubprocessConfig` are all deleted. The generic `mngr_forward --allow-host-loopback` CLI flag stays for non-minds users.
- The FCT-side `[create_templates.dev]` block (worktree-base override + UV-tool provisioning + DEV-only `pass_env`) is deleted in the same `mngr/tweak-template` FCT branch that the previous task already opened.
- The already-skipped `test_create_agent_dev_mode_e2e` is deleted with the rest; no LIMA-mode replacement.

## Expected Behavior

- The web create form's "Compute provider" dropdown no longer offers `dev`; it shows only `local`, `cloud`, `lima`, `imbue_cloud`.
- POSTing `launch_mode=DEV` to `/create` or `/api/create-agent` produces the existing `Invalid launch_mode` 400 path (handled by the `LaunchMode(...)` constructor in `app.py`).
- Setting `MINDS_ALLOW_HOST_LOOPBACK=1` in minds' env has no effect (the var is no longer read). `mngr forward --allow-host-loopback` still works for any non-minds caller.
- `mngr create --template main --template dev` from inside the FCT now fails with mngr's standard "Template 'dev' not found" `UserInputError`, which is the desired behaviour for an explicitly-removed mode.
- All existing non-DEV launch flows are unchanged: same `mngr create` command shape, same latchkey wiring, same secret forwarding.

## Changes

- `apps/minds/imbue/minds/primitives.py`: drop `DEV` from the `LaunchMode` enum.
- `apps/minds/imbue/minds/desktop_client/agent_creator.py`: drop both `case LaunchMode.DEV` branches in `_build_mngr_create_command` (address builder + per-mode template/runtime block); drop the `is not LaunchMode.DEV` gate around the constant-port latchkey URL fallback; delete `_maybe_compute_latchkey_gateway_url` and inline its only call site to use the constant agent-side URL; clean up DEV references in surrounding docstrings/comments.
- `apps/minds/imbue/minds/desktop_client/forward_cli.py`: drop the `allow_host_loopback` field on `ForwardSubprocessConfig` and the `if config.allow_host_loopback: command.append("--allow-host-loopback")` line in the subprocess command builder; clean up the DEV-mention in the field docstring (now gone).
- `apps/minds/imbue/minds/cli/run.py`: drop the `MINDS_ALLOW_HOST_LOOPBACK` lookup and the `allow_host_loopback=...` kwarg passed to `ForwardSubprocessConfig`; clean up the DEV-only comment.
- `apps/minds/imbue/minds/desktop_client/templates_test.py`: remove `test_render_create_form_selects_specified_launch_mode` (asserts `value="DEV" selected`); the existing "all launch modes are rendered" test continues to cover the remaining four modes.
- `apps/minds/imbue/minds/desktop_client/agent_creator_test.py`: remove the DEV-only tests (`..._omits_latchkey_for_dev_mode_without_url`, `..._injects_latchkey_for_dev_mode_with_explicit_url`, `..._omits_latchkey_password_for_dev_mode`, `..._omits_latchkey_jwt_for_dev_mode`) and drop the `(LaunchMode.DEV, None)` row from the `_never_inlines_secret_env_flags` parametrization; tighten `..._disables_latchkey_counting_when_wired` to drop its DEV branch.
- `apps/minds/imbue/minds/desktop_client/test_desktop_client.py`: remove `test_create_form_submit_passes_launch_mode` and `test_create_agent_api_passes_launch_mode` (both pin DEV through the request path); leave `test_create_agent_api_rejects_invalid_launch_mode` (it uses `INVALID_MODE` and is unaffected).
- `apps/minds/test_desktop_client_e2e.py`: delete the skipped `test_create_agent_dev_mode_e2e` and `_DEV_AGENT_NAME` constant.
- `apps/minds/docs/{design.md,overview.md,user_story.md,workspace/glossary.md}`: drop DEV-mode mentions; the surviving descriptions list `LOCAL`, `LIMA`, `CLOUD`, `IMBUE_CLOUD`.
- `.external_worktrees/forever-claude-template/.mngr/settings.toml`: delete the `[create_templates.dev]` block in its entirety; this lands on the existing `mngr/tweak-template` FCT branch.
