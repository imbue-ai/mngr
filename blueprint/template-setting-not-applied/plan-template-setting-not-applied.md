# Plan: Fix create-template `setting`/`setting__extend` entries being silently dropped (issue #1914)

## Overview

- **Bug:** A create template can declare `setting__extend = ["providers.docker.docker_runtime=runsc"]`. The value flows into the create command's `--setting` param but never reaches the resolved `MngrConfig`, so it has no effect. Direct CLI `-S/--setting` works; only template-provided settings are lost.
- **Root cause:** In `libs/mngr/imbue/mngr/cli/common_opts.py`, `setup_command_context` applies settings to the config exactly once (lines 209-217, `apply_settings_to_config(... initial_opts.setting ...)`), and does so *before* `apply_create_template` runs (line 234). The template appends its `setting__extend` entries into `updated_params["setting"]` *after* that single application point, so they land in `opts.setting` but are never merged into `mngr_ctx.config`. `get_provider_instance` reads provider config from `mngr_ctx.config`, which never saw them.
- **Why latent so long:** The only prior template `setting__extend` provider use was `providers.docker.is_enabled=true`, which is a no-op regardless of this bug — `get_provider_instance` (`api/providers.py:57`) resolves an explicitly-named provider directly from `config.providers[name]` with no `is_enabled` gate (that gate lives only in `list_provider_names_to_load`, `:166`). `docker_runtime` is the first template-set provider field whose absence is observable.
- **Fix approach (decided in Q&A):** Keep the existing line-209 CLI `-S` application so `apply_config_defaults`/`apply_create_template` still see CLI settings. Then, at the end of the pipeline (create only), re-run `apply_settings_to_config` on the **originally-loaded** config with the combined `template-then-CLI` setting list, replacing the config returned in `mngr_ctx`. This preserves "CLI `-S` wins over template" precedence and avoids double-applying `__extend` (because we re-apply against the original config, not the already-modified one).
- **Guardrail:** Template-contributed `setting` keys that target `commands.*` or `create_templates.*` cannot take effect (those are consumed *before* the re-application point), so they raise `ConfigParseError` rather than failing silently. Validation is scoped to template-contributed entries only — direct CLI `-S commands.*` keeps working.
- **Secondary fix:** `config get` and `config list --all` cannot surface provider-subclass fields (e.g. `docker_runtime`) because `MngrConfig.providers` is typed as the base `dict[..., ProviderInstanceConfig]` and `model_dump(mode="json")` serializes by declared type, dropping subclass-only fields. Add `serialize_as_any=True` at the two `config.py` call sites. This made the bug hard to diagnose (the natural probe reported "Key not found").

## Expected behavior

- A create template with `setting__extend = ["providers.docker.docker_runtime=runsc"]` results in the resolved `mngr_ctx.config.providers["docker"].docker_runtime == "runsc"`, and the launched container runs under `--runtime runsc` (matching `mngr create ... -S providers.docker.docker_runtime=runsc`).
- Bare-assign template settings (`setting = ["..."]`) and `setting__extend` both reach the config, with the same parse/merge semantics as `--setting`.
- Direct CLI `-S` still wins over a template-provided setting for the same key (e.g. template sets `docker_runtime=runsc`, CLI `-S providers.docker.docker_runtime=runc` → resolved value is `runc`).
- Template settings are subject to the same settings-narrowing guard as `--setting`: a template `setting` assignment that would narrow a non-empty list/dict/set raises `ConfigParseError` unless `allow_settings_key_assignment_narrowing` is enabled.
- A template `setting`/`setting__extend` targeting `commands.*` or `create_templates.*` raises `ConfigParseError` naming the template and key and pointing the user at the `[commands.*]`/`[create_templates.*]` config sections. (Direct CLI `-S` for these keys is unaffected.)
- A template `setting` targeting `allow_settings_key_assignment_narrowing` hits the existing rejection in `apply_settings_to_config` (the flag can't be set this way), consistent with `--setting`.
- `mngr config get providers.docker.docker_runtime` and `mngr config list --all` now show provider-subclass fields that are set, instead of "Key not found".
- Non-`create` commands are unchanged: only the `create` path runs the re-application/validation.

## Implementation plan

### `libs/mngr/imbue/mngr/cli/common_opts.py`

- **`setup_command_context`** (the core change):
  - Before line 209, capture a reference to the originally-loaded config (the `mngr_ctx.config` value prior to the CLI `-S` merge) — call it `base_config`. The existing line-209 block continues to merge `initial_opts.setting` into `mngr_ctx.config` so `apply_config_defaults` (line 225) and `apply_create_template` (line 234) keep seeing CLI settings.
  - After `restore_cli_list_values` (line 238) and only when `command_name == "create"`:
    - Compute the combined setting list from the post-pipeline params: `combined_settings = updated_params.get("setting", ())`. Its ordering is `template_entries + cli_entries` (config base for `setting` is `()`, templates extend, then `restore_cli_list_values` appends the CLI tail).
    - Isolate the template-contributed slice by trimming the known CLI tail: `cli_count = len(initial_opts.setting)`; `template_settings = combined_settings[: len(combined_settings) - cli_count]`. (Asserted-consistent: the trailing `cli_count` entries equal `initial_opts.setting`.)
    - If `template_settings` is non-empty:
      - Validate each template setting key via a new helper `_reject_ineffective_template_setting_keys(template_settings, template_names)`; raise `ConfigParseError` if any key path's first segment is `commands` or `create_templates`.
      - Re-apply: `final_config = apply_settings_to_config(base_config, combined_settings, base_config.disabled_plugins)` and update `mngr_ctx` with `to_update(mngr_ctx.field_ref().config, final_config)`.
  - Net effect: for create with template settings, the returned config is recomputed from the original config with `template-then-CLI` settings applied once; for create without template settings and for all other commands, behavior is identical to today.
- **New helper `_reject_ineffective_template_setting_keys(template_settings, template_names)`**:
  - Parse each `KEY=VALUE` (reuse the same split logic shape as `apply_settings_to_config`), take the dotted key path, and reject when the first segment is in a module-level constant `_TEMPLATE_INEFFECTIVE_SETTING_PREFIXES = frozenset({"commands", "create_templates"})`.
  - Error message (per Q&A): names the offending template(s) and key, explains it cannot take effect via a template `setting` because those sections are resolved before template settings are applied, and suggests writing it directly under the `[commands.*]` / `[create_templates.*]` config section.
- **`apply_create_template` docstring:** no behavior change, but its existing contract ("the same operator suffix recognised in TOML, `--setting`, and env vars") is now actually honored for `setting`; leave the docstring accurate (it already promises this).

### `libs/mngr/imbue/mngr/cli/config.py`

- **`_config_get_impl`** (line 447): change `mngr_ctx.config.model_dump(mode="json")` to `mngr_ctx.config.model_dump(mode="json", serialize_as_any=True)`.
- **`_config_list_impl`** (line 253, the `--all` `full_view`): same `serialize_as_any=True` addition so the full dump includes provider-subclass fields.

### Changelog

- Add `libs/mngr/changelog/mngr-fix-settings-bug-ticket.md` describing: template `setting`/`setting__extend` now apply to the resolved config; new error for `commands.*`/`create_templates.*` template settings; `config get`/`list --all` now surface provider-subclass fields. (Single-project PR — only `libs/mngr` is touched.)

## Implementation phases

1. **Core re-application.** Capture `base_config`, add the create-only post-pipeline re-application of combined settings onto `base_config`, update `mngr_ctx.config`. Verify manually that a docker create-template `setting__extend` reaches `mngr_ctx.config` and the emitted `docker run` includes `--runtime`. System works for the happy path; validation/secondary fix not yet present.
2. **Forbidden-key validation.** Add `_reject_ineffective_template_setting_keys` + the prefix constant + error message, wired before re-application. System now errors loudly instead of silently dropping `commands.*`/`create_templates.*` template settings.
3. **`config get`/`list` traversal fix.** Add `serialize_as_any=True` at the two `config.py` call sites. The diagnostic probe now works.
4. **Tests + changelog.** Add the unit tests below and the changelog entry. Run the full suite.

## Testing strategy

- **Unit (`libs/mngr/imbue/mngr/cli/common_opts_test.py`):**
  - Template `setting__extend` lands in resolved config: drive `setup_command_context` for `create` via `CliRunner` (model on `test_setting_flag_overrides_config_via_setup_command_context` / `test_headless_flag_..._via_setup_command_context`) with a `create_templates.<name>` defining `setting__extend = ["providers.docker.docker_runtime=runsc"]`, and assert `mngr_ctx.config.providers["docker"].docker_runtime == "runsc"`.
  - Bare-assign template `setting` (non-extend) also reaches config.
  - CLI `-S` wins over a template setting for the same key (template `runsc`, CLI `-S ...=runc` → `runc`).
  - Template `setting` narrowing assignment raises `ConfigParseError` without the opt-in (parallels `test_apply_settings_to_config_narrowing_raises_by_default`).
  - Template `setting` targeting `commands.create.connect=false` raises `ConfigParseError`; the message names the template/key.
  - Template `setting` targeting `create_templates.foo...` raises `ConfigParseError`.
  - Direct CLI `-S commands.create.connect=false` still works (regression guard for the validation being template-scoped).
  - Template `setting` targeting `allow_settings_key_assignment_narrowing` hits the existing rejection (parallels `test_apply_settings_to_config_rejects_setting_the_narrowing_flag`).
- **Unit (`libs/mngr/imbue/mngr/cli/config_test.py`):**
  - `config get providers.<name>.docker_runtime` returns the value for a config containing a `DockerProviderConfig` with `docker_runtime` set (previously "Key not found").
  - `config list --all` includes the provider-subclass field.
- **Edge cases to cover:** multiple `--template`s each contributing settings (stacked, later wins per `merge_with`); a create with both template settings and CLI `-S`; create with no template (re-application is a no-op / behavior unchanged); non-create command (`config`, etc.) unaffected.
- **Full run:** `just test-offload` for the final check (acceptance tests run in CI). Iterate locally with `just test-quick "libs/mngr/imbue/mngr/cli/common_opts_test.py"` and the `config_test.py` path.

## Open questions

- **Slice-trim assumption:** isolating template settings by trimming `len(initial_opts.setting)` from the tail of `updated_params["setting"]` relies on the pipeline ordering (config base `()` + template + CLI). Should we instead have `apply_create_template` return the template-contributed setting entries explicitly for robustness, or is an internal consistency assertion on the CLI tail sufficient? (Current plan: keep the trim with an assertion.)
- **`serialize_as_any` blast radius:** scoped to the two `config.py` call sites per Q&A. Confirm no other read path (e.g. structured `config get --format json` consumers) depends on the base-type-only projection.
- **Acceptance coverage:** Q&A chose unit tests only for the core fix. Is a release/acceptance test that does a real `create --template` with docker and asserts `--runtime runsc` desired later, or is the unit-level assertion on resolved config sufficient?
