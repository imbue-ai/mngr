# imbue-mngr-antigravity

Plugin that registers the `antigravity` agent type for mngr.

[Antigravity CLI](https://antigravity.google/docs/cli-overview) is Google's terminal-based AI coding assistant, the successor to the legacy Gemini CLI. Google announced on 2026-05-19 that Gemini CLI is being replaced by Antigravity CLI, with the legacy request path turning off for paid-tier accounts on 2026-06-18. This plugin lets you run the new CLI (`agy`) as an mngr agent.

## Usage

```bash
mngr create my-agent antigravity
```

Pass arguments to the `agy` command with `--`:

```bash
mngr create my-agent antigravity -- --help
```

## Configuration

Define a custom variant in your mngr config (`mngr config edit`):

```toml
[agent_types.my_agy]
parent_type = "antigravity"
cli_args = "--add-dir=/extra/dir"
```

Then create agents with your custom type:

```bash
mngr create my-agent my_agy
```

`auto_allow_permissions = true` adds `--dangerously-skip-permissions` to the launch command so every tool call is auto-approved without prompting.

## Caveats

- **`agy` PATH shadowing**: if the Antigravity 2.0 desktop app is installed, its bundled `agy` shim can shadow the standalone CLI in `PATH`. Remove the desktop app's `bin/agy` or override `command` with an absolute path to the Go binary.
- **First-launch trust dialog**: each fresh `work_dir` triggers Antigravity's "Do you trust this folder?" gate. There is no `GEMINI_CLI_TRUST_WORKSPACE` equivalent env var, so the dialog must be accepted once per workspace; subsequent launches in the same dir do not re-prompt.
- **No `mngr transcript` support yet**: Antigravity stores conversations as protobuf `.pb` files (under `~/.gemini/antigravity-cli/conversations/`) whose schema has not been validated against an authenticated CLI. Transcript scripts that the legacy `mngr_gemini` plugin shipped have been dropped pending a follow-up spike.
- **No readiness sentinel**: readiness is signalled purely by polling the rendered TUI banner. The hook-based sentinel that `mngr_gemini` used has not been re-introduced because the Antigravity hook JSON schema has not been empirically validated.

See the [mngr agent types documentation](https://github.com/imbue-ai/mngr/blob/main/libs/mngr/docs/concepts/agent_types.md) for more details.
