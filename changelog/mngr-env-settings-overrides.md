## Breaking: unified settings overrides

Any mngr config field can now be overridden from a single, unified mechanism:

- **`MNGR__X__Y__Z=value` env vars** (note the double underscores) target the dotted path `x.y.z`. This replaces the narrow `MNGR_COMMANDS_<CMD>_<PARAM>` scheme and frees plugin / CLI command names to contain multiple words. `MNGR_COMMANDS_*` is **removed**.
- **`--setting x.y.z=value`** and **`mngr config set x.y.z value`** continue to work and now go through the same resolver.
- **`__extend` operator suffix on a leaf key** (e.g. `MNGR__AGENT_TYPES__MY_CLAUDE__CLI_ARGS__EXTEND='["--model","opus"]'`) opts into additive behavior: append for lists/tuples, shallow key-merge for dicts, union for sets. The bare key is always assignment.
- **`mngr config extend KEY VALUE`** writes the `__extend` form; **`mngr config set KEY__extend VALUE`** is accepted as an alias.
- **`mngr config schema`** lists every settable key with type and current effective value; **`mngr config list --all`** includes default-valued fields too.

### Breaking changes you'll notice

- **Layer merging is now assign-by-default for every aggregate** (list, tuple, dict, set). Older configs that relied on implicit concat across user/project/local files (e.g. `cli_args` accumulating) now need an explicit `cli_args__extend = [...]` to keep the additive behavior. The five top-level container dicts on `MngrConfig` (`agent_types`, `providers`, `plugins`, `commands`, `create_templates`) keep their per-key merge — adding `[agent_types.foo]` in one scope still doesn't drop another scope's `[agent_types.bar]`. `disabled_plugins` is a separate carveout: it is populated by `--disable-plugin` CLI flags rather than TOML files, and an empty override preserves the base value (use `[plugins.<name>] enabled = false` to disable a plugin per-scope).
- **Agent-type parent-type inheritance** likewise stops auto-concatenating `cli_args` / `extra_provision_command` / `upload_file` / `create_directory` / `env` / `env_file`. Use `field__extend` to inherit-and-extend.
- **Removed env vars:** `MNGR_COMMANDS_<CMD>_<PARAM>`, `MNGR_ENABLE_PARAMIKO_LOGGING`, `MNGR_AGENT_READY_TIMEOUT`. These are promoted to first-class config fields (`logging.enable_paramiko_logging`, `agent_ready_timeout`) and remain settable via `MNGR__*`.
- **`MNGR_COMPLETION_CACHE_DIR` stays as-is** (single underscore). It's read by the tab-completion lightweight pre-reader path that intentionally skips full config loading, so it joins the "special" env vars (`MNGR_ROOT_NAME` / `MNGR_PREFIX` / `MNGR_HOST_DIR`) rather than becoming a config field. The double-underscore `MNGR__COMPLETION_CACHE_DIR` form is not recognised.
- **Renamed:** `MNGR_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE` → `MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE`.
- **Preserved aliases:** `MNGR_ROOT_NAME`, `MNGR_HOST_DIR`, `MNGR_PREFIX`, and `MNGR_HEADLESS` continue to work. Setting both an alias and its canonical `MNGR__*` form to different values raises `ConfigParseError`.
- **Field name restrictions:** field names can no longer contain `__` (reserved as the env-var segment separator and `__extend` operator). Sibling keys that lowercase-collapse to the same env-var segment now raise at config-load time.

No compatibility shim is provided; the major-version bump is the migration signal.
