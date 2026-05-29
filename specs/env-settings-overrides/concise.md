# Dynamic `MNGR__*` env-var overrides and unified assign-vs-extend semantics

## Overview

- Lets any mngr config setting be overridden via environment variables, replacing the narrow `MNGR_COMMANDS_<CMD>_<PARAM>` scheme with `MNGR__X__Y__Z` (double-underscore segments) that map 1:1 to the dotted TOML / `--setting` path `x.y.z`.
- Reuses the existing dotted-key resolver behind `--setting` (`apply_settings_to_config` in `libs/mngr/imbue/mngr/cli/common_opts.py`) so there is one shared key-parsing pipeline across TOML, env vars, `--setting`, and `mngr config <verb>`.
- Introduces an explicit `__extend` suffix on the leaf key for "append-list / merge-dict / union-set" semantics. The bare key is *always* assignment. This is uniform across all four entry points and ships as a deliberate breaking change to the layer-merge rules (no compatibility shim; major-version bump signals the break).
- Frees plugin- and CLI-command names to contain multiple words (e.g. `my-agent`, `mngr-pair`) without colliding with env-var path parsing, since segment boundaries are now unambiguous.
- Promotes several inline-read `MNGR_*` env vars to first-class config fields, renames the lingering debug flag for clarity, and preserves a short list of "special" env vars (those that affect *which* config gets loaded, or are mngr-set runtime metadata for agents).

## Expected Behavior

### `MNGR__*` env vars

- `MNGR__X__Y__Z=v` is exactly equivalent to `--setting x.y.z=v` and to writing `x.y.z = v` in `settings.toml`.
- Path segments are uppercase only (`[A-Z0-9_]+`), and `__` is the segment separator. Lowercase letters in the suffix are rejected.
- Segments are lowercased to produce the canonical config key.
- The old `MNGR_COMMANDS_<CMD>_<PARAM>=v` parsing is removed — `MNGR__COMMANDS__<CMD>__<PARAM>=v` replaces it. The single-word-command-name restriction goes away.
- Values are parsed the same way `--setting` parses them: JSON first, fall back to raw string. The "empty value clears a tuple/list" behavior currently in `apply_config_defaults` is preserved.
- Strict-mode handling is governed by the existing `MNGR_ALLOW_UNKNOWN_CONFIG` policy, symmetric with TOML. An unknown `MNGR__*` key raises `ConfigParseError` by default; setting `MNGR_ALLOW_UNKNOWN_CONFIG=1` downgrades it to a warning.
- Precedence: built-in defaults → user TOML → project TOML → local TOML → `MNGR__*` env vars → `--setting` → CLI args → click defaults filled from `commands.<cmd>`.
- `MNGR__*` env vars are applied late in `load_config`, after plugin discovery and after TOML layer merging, before the `on_load_config` plugin hook. Disabled plugins' blocks are skipped, consistent with TOML parsing.

### Preserved old-style env vars

- `MNGR_ROOT_NAME`, `MNGR_HOST_DIR`, `MNGR_PREFIX`, and `MNGR_HEADLESS` continue to be accepted in their existing flat form.
- `MNGR_HEADLESS` is read early and synthesized internally into a `MNGR__HEADLESS` mapping that is then applied via the shared resolver. The three host/root specials likewise route through the shared pipeline (they remain "special" only in that they're read before config-file resolution).
- If both an old-style alias and the new canonical form are set with *different* values (e.g. `MNGR_PREFIX=a` and `MNGR__PREFIX=b`), `load_config` raises `ConfigParseError` and refuses to start. Same value is fine.

### Other `MNGR_*` env var dispositions

- Unchanged (config-loading meta / mngr-set runtime metadata): `MNGR_PROJECT_CONFIG_DIR`, `MNGR_ALLOW_UNKNOWN_CONFIG`, `MNGR_LOAD_ALL_PLUGINS`, `MNGR_USER_ID`, `MNGR_TEST_VERBOSE`, and the agent-runtime vars (`MNGR_AGENT_ID`, `MNGR_AGENT_NAME`, `MNGR_AGENT_STATE_DIR`, `MNGR_AGENT_WORK_DIR`, `MNGR_HOST_DIR` set inside agents, `MNGR_GIT_BASE_BRANCH`).
- Promoted to first-class config fields and read via `MNGR__*` only:
  - `MNGR_AGENT_READY_TIMEOUT` → new top-level field `MngrConfig.agent_ready_timeout: float`.
  - `MNGR_COMPLETION_CACHE_DIR` → new top-level field `MngrConfig.completion_cache_dir: Path | None`.
  - `MNGR_ENABLE_PARAMIKO_LOGGING` → new field `LoggingConfig.enable_paramiko_logging: bool`.
- Renamed for clarity: `MNGR_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE` → `MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE`. Same semantics; new name signals "debug-only."
- Plugin env vars stay as-is (`MNGR_MODAL_APP_NAME` and `MNGR_MODAL_APP_BUILD_PATH` in `mngr_modal/routes/snapshot_and_shutdown.py`, where module-level import restrictions block reading mngr config; `MNGR_MODAL_DISABLE_SNAPSHOT_DEPLOY` debug toggle; `MNGR_ROOT_NAME` propagation in `mngr_schedule/plugin.py`).

### Assign vs extend

- Bare key → *assign*: the value replaces whatever the lower-precedence layers produced.
- Leaf key with `__extend` suffix → *extend*: append for `list[T]` and `tuple[T, ...]` (concatenation), shallow key-merge for `dict[K, V]` (extender's keys win on collision), and union for `frozenset[T]`. No recursion into nested aggregates; `__extend` operates only at the field it's attached to.
- `__extend` on a *scalar* field raises `ConfigParseError`. Silent fallback would mask typos.
- A `__extend` value whose JSON shape doesn't match the target field's aggregate type raises `ConfigParseError` with a clear message naming the field and expected shape. Scalars are *not* auto-wrapped into one-element lists.
- Case convention is context-matched: `__EXTEND` in env-var paths (uppercase, matching all `MNGR__*` segments) and `__extend` everywhere else (TOML keys, `--setting`, `mngr config <verb>`).
- Within a single layer, a bare assignment is always processed *before* a sibling `__extend` operation. So `cli_args = []` plus `cli_args__extend = ["a"]` in the same file resolves to `["a"]` and gives a clean "reset earlier-precedence value, then add these" idiom.
- Across precedence layers, `__extend` operations stack additively. With user `cli_args__extend = ["a"]`, project `cli_args__extend = ["b"]`, and `MNGR__CLI_ARGS__EXTEND='["c"]'`, the resolved value is `["a", "b", "c"]`.

### Breaking change to `merge_with`

- Every aggregate field flips to assign-by-default in `MngrConfig.merge_with`, `AgentTypeConfig.merge_with`, and the related provider/plugin/template merges. The named concat-tuple fields on `AgentTypeConfig` (`cli_args`, `extra_provision_command`, `upload_file`, `create_directory`, `env`, `env_file`) lose their auto-concat behavior; parent-type-inherited args must be re-added via `__extend` if desired. `unset_vars` and `enabled_backends` similarly flip to plain assign. `disabled_plugins: frozenset[str]` flips to assign.
- **Carveout:** the top-level *container* dicts on `MngrConfig` (`agent_types`, `providers`, `plugins`, `commands`, `create_templates`) keep their per-key additive merge across TOML layers — adding `[agent_types.proj_codex]` at project scope still preserves a user-scope `[agent_types.my_claude]`. Their *leaf* values still flip to assign-by-default (so `commands.create.connect = false` at project scope replaces, not merges with, a user-scope `commands.create.connect = true`).
- `pre_command_scripts` and `work_dir_extra_paths` are *not* in the carveout: they flip to assign-by-default like other leaf dicts. Users opt into additive merging via `__extend`.
- An old mngr version reading a new-style TOML with `cli_args__extend = [...]` fails loudly (unknown field) under default strictness, or logs a warning and drops the field when `MNGR_ALLOW_UNKNOWN_CONFIG=1`. We accept this as the correct major-version-bump signal; no soft-migration tooling.

### Sibling-key collision detection

- `_normalize_field_keys` is extended so that two sibling keys at the same dict level that normalize to the same env-var-segment form (e.g. `my-agent` and `my_agent` under the same parent, both lowercasing to `my_agent`) raise `ConfigParseError` at config-load time. This eagerly prevents ambiguity in `MNGR__*` lookups.
- Field names that literally contain `__` are forbidden at registration time — `_normalize_field_keys` and the user-defined-name validators (agent type, provider, plugin, template) reject any key containing `__`. Keeps the env-var encoding unambiguous and round-trippable.
- Reserved env-var keyword: when the `__EXTEND` suffix would conflict with a plugin/config field literally named `extend`, the schema wins — if the parent has a real `extend` field, the segment resolves to that field; otherwise (parent is an aggregate) it acts as the extend suffix. Disambiguation is schema-driven, not lexical.

### `mngr config` CLI updates

- `mngr config set KEY VALUE [--scope]`: bare-key assignment, as today. Additionally accepts a key whose final segment is literally `__extend` and routes it through the same code path as `mngr config extend`.
- New `mngr config extend KEY VALUE [--scope]`: writes an `__extend` operation. Requires the target leaf field to be an aggregate (list/tuple/dict/frozenset); otherwise errors. Default `--scope` mirrors `set` (project).
- `mngr config set` and `mngr config extend` reject list/dict values written without explicit `set` vs `extend` intent — i.e., to mutate an aggregate field, the user must consciously pick a verb.
- `mngr config unset KEY [--scope]`: literal TOML-key removal. `mngr config unset cli_args__extend` removes only that key; `mngr config unset cli_args` removes only the bare key. To clear both, run twice.
- `mngr config get KEY --scope <s>` for an `__extend`-only key in scope: human format prints the value with a sentinel ellipsis, e.g. `[..., "--foo"]` for `cli_args__extend = ["--foo"]`, visually marking that it's an extend operation rather than a full assignment. JSON / JSONL format emits the literal TOML key: `{"key": "cli_args__extend", "value": ["--foo"]}` — what's in the file is what's emitted.
- `mngr config get KEY` (no `--scope`) always returns the resolved plain value from the merged config. `__extend` keys and the ellipsis sentinel never appear in the merged view, since extends are applied by the time the merge completes.
- New `mngr config schema`: prints every settable key path with its type and current effective value, sourced from `MngrConfig.model_fields` (recursively, through enabled plugins' config classes). Stops at the dict level for open-ended `dict[str, Any]` fields (e.g. `commands.<cmd>.defaults: dict[str, Any]`) — does not enumerate user-extensible subkeys.
- New `mngr config list --all` flag: extends the existing `list` subcommand to include keys with their default values, not just keys explicitly present in the merged config. Distinct from `schema` (which is type-annotated, value-secondary); `list --all` is value-focused.
- Both `mngr config schema` and `mngr config list --all` reflect *enabled* plugins only.

### `on_load_config` hook contract

- Pluggy plugins receive a fully resolved `config_dict` from `on_load_config` — all `__extend` operations have been applied before the hook fires. Plugins always see plain field shapes; they never need to understand the operator. Plugins remain free to compose lists/dicts themselves in Python.

### Documentation

- `libs/mngr/docs/concepts/environment_variables.md` is rewritten to describe the new `MNGR__*` scheme, list the preserved old-style env vars, document the `__extend` suffix, and explain the new precedence chain.
- `libs/mngr/docs/commands/secondary/config.md` is updated for the new `extend` and `schema` subcommands and the `list --all` flag.
- `changelog/mngr-env-settings-overrides.md` is created with a user-visible summary, called out explicitly as a breaking change.
- No separate migration guide; the major-version bump and changelog entry are the signal.

## Changes

- Remove the `MNGR_COMMANDS_<CMD>_<PARAM>` parser and the `_ENV_COMMANDS_PREFIX` constant in `libs/mngr/imbue/mngr/config/loader.py`, along with the "single-word command name" comment block.
- Add a new shared key-resolver module (or augment `libs/mngr/imbue/mngr/cli/common_opts.py`) that takes a list of `(key_path, value)` pairs — where `key_path` may end in `__extend` — and applies them onto a raw config dict, distinguishing assign vs extend at parse time. This resolver is the single shared entry point invoked by:
  - The `MNGR__*` env-var application step in `load_config`.
  - `apply_settings_to_config` (the `--setting` flag).
  - `mngr config set/extend` and `mngr config unset`.
  - The TOML parser (so `cli_args__extend = [...]` works in any settings file).
- Add the `MNGR__*` env-var parser in `libs/mngr/imbue/mngr/config/loader.py`: scan `os.environ` for keys matching `^MNGR__[A-Z0-9_]+(__[A-Z0-9_]+)*$`, validate uppercase-only and segment shape, lowercase to produce dotted key paths, JSON-parse values, and feed them into the shared resolver. Wire this in at the same point the old `_parse_command_env_vars` lived (after TOML merge, before `on_load_config`).
- Add the `MNGR_HEADLESS` (and any other preserved-alias) synthesis step: read the var once, translate to an `MNGR__HEADLESS` mapping, push into the same resolver path. Detect conflicts between alias and canonical form and raise `ConfigParseError`.
- Rework `MngrConfig.merge_with`, `AgentTypeConfig.merge_with`, `ProviderInstanceConfig.merge_with`, `PluginConfig.merge_with`, `CommandDefaults.merge_with`, `CreateTemplate.merge_with`, `RetryConfig.merge_with`, and `LoggingConfig.merge_with` so that every aggregate field is plain assign-by-default, except the carveout: top-level container dicts on `MngrConfig` retain per-key additive merge. Delete `AGENT_TYPE_CONCAT_TUPLE_FIELDS`, the bespoke "non-empty override" rule for `enabled_backends`, and the union for `disabled_plugins`.
- Extend `_normalize_field_keys` (and the agent-type / provider / plugin / template name validators) to:
  - Raise on sibling keys that normalize to the same env-var-segment form at the same dict level.
  - Raise on any key that literally contains `__`.
- Add `__extend` recognition to the shared resolver: when the leaf segment is `__extend` (CLI/TOML) or `__EXTEND` (env), pop the suffix, look up the target field's annotated type, and apply the type-specific extend operation. Within a single layer, apply assignment first, then sibling `__extend` operations. Across layers, stack additively.
- Add a new top-level field `MngrConfig.agent_ready_timeout: float` (with the existing default), `MngrConfig.completion_cache_dir: Path | None`, and `LoggingConfig.enable_paramiko_logging: bool`. Replace the inline `os.environ.get(...)` reads at the affected callsites (`libs/mngr/imbue/mngr/interfaces/host.py`, `libs/mngr/imbue/mngr/config/completion_cache.py`, `libs/mngr/imbue/mngr/utils/logging.py`) with config lookups.
- Rename `MNGR_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE` → `MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE` at its single read site in `libs/mngr/imbue/mngr/hosts/host.py`.
- Update `libs/mngr/imbue/mngr/cli/config.py`:
  - Add a new `config_extend` Click subcommand with the same options as `config_set` plus the aggregate-type validation against the schema.
  - Update `config_set` to detect a trailing `__extend` on the key and route to the extend implementation.
  - Add validation that rejects list/dict values when neither `set` nor `extend` was used unambiguously.
  - Add the `config_schema` subcommand backed by introspection of `MngrConfig.model_fields` (recursively, through enabled plugins' registered config classes).
  - Add a `--all` flag to `config_list` that includes default-valued fields.
  - Update `_unset_nested_value` callers to handle the literal `__extend` suffix as just another key segment (no special behavior).
  - Update help-metadata blocks for `set`, the new `extend`, and `schema`.
- Update `libs/mngr/imbue/mngr/cli/common_opts.py` so `--setting` accepts `key__extend=value` and routes through the same shared resolver.
- Update `libs/mngr/imbue/mngr/cli/help_formatter.py` (or wherever option-help mentions `MNGR_HEADLESS` / `MNGR_COMMANDS_*`) so help strings reflect the new env-var scheme.
- Drop the inline `os.environ["MNGR_HEADLESS"]` handling in `load_config` (it now flows through the alias-synthesis step) and the corresponding inline read in `data_types.py:get_or_create_user_id` stays as-is (it's not a config field).
- Rewrite `libs/mngr/docs/concepts/environment_variables.md` end-to-end. Update `libs/mngr/docs/commands/secondary/config.md`. Add `changelog/mngr-env-settings-overrides.md`.
- Add unit and integration tests for: (a) `MNGR__*` parse-and-apply, including uppercase validation, JSON-vs-string parsing, and empty-value-clears-tuple semantics; (b) `__extend` on each aggregate type and the `ConfigParseError` for scalar/shape-mismatch cases; (c) within-layer assign-before-extend ordering; (d) cross-layer extend stacking; (e) the alias-conflict detection (`MNGR_PREFIX` vs `MNGR__PREFIX`); (f) the sibling-key normalization collision; (g) the field-name-contains-`__` rejection; (h) the carveout — top-level container dicts still merge additively, leaf dicts assign; (i) `mngr config schema`, `mngr config extend`, `mngr config list --all`, and `mngr config get --scope` rendering for `__extend` keys (human and JSON formats).
- Update existing tests that depend on the old `MNGR_COMMANDS_*` form or on the old concat-merge semantics. This is expected to touch many config / loader / cli tests; the breaking change is intentional.
