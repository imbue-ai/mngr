# Simplify --command / --type interaction

## Problem

When a user's settings.toml sets `type` under `[commands.create]`, using `--command` on the CLI fails with a mutual exclusivity error -- even though the user never passed `--type` on the command line. The error message references `--type` without indicating it came from config.

User impact: forces explicit `--type=generic` on every `--command` invocation when config sets a default type. Confusing error when the user didn't pass the flag being complained about.

## How it works today

1. CLI parses `--type` and `--command` independently (`create.py:242-246`)
2. `apply_config_defaults()` merges `[commands.create]` config values into any parameter with source == DEFAULT (`common_opts.py:413-460`). After merging, source info is lost.
3. `_parse_agent_opts()` checks: if `--command` is set and `resolved_agent_type` is not None and not "generic", raise error (`create.py:1329-1337`)
4. `assemble_command()` in `base_agent.py:93-120` already supports `command_override` on any agent type -- the mutual exclusivity is enforced only at the CLI layer

## Options

### A: Track parameter source, let CLI win over config
Only error when both `--type` and `--command` are explicitly on the CLI. When `--type` came from config, `--command` overrides it to "generic" silently. Requires threading `ParameterSource` info into `_parse_agent_opts`.

### B: Make --command orthogonal to --type
Remove mutual exclusivity entirely. `--command` overrides the command; `--type` controls agent config (provisioning, env, permissions). They're independent. If `--command` without `--type`, type defaults to "claude" (normal default), not "generic." The "generic" type is just another type.

### C: Improve error message only
Keep mutual exclusivity but show where each value came from: "You passed --command on CLI, --type=pi is set in ~/.mngr/settings.toml."

### D: Remove --command entirely
Users would use `mngr create name --type generic -- cmd args...` or positional agent type. Breaking change; less ergonomic for complex commands.

### E: Hybrid source-aware behavior
`--command` without any `--type` -> generic. `--command` with CLI `--type` -> use that type + override command. `--command` with config `--type` -> generic. Most flexible but hardest to document.

### F: Rename --command to --run with new semantics
Rename signals semantic change. Adopt Option B behavior under new name. Clean break but disruptive.

## Recommendation

Option A (source tracking) as minimal fix, or Option B (orthogonal) for cleaner long-term design. Option B is supported by the fact that `assemble_command` already handles command overrides on any agent type -- the guard is CLI-only and not load-bearing.
