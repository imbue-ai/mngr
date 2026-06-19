# imbue-mngr-opencode

Plugin that registers the `opencode` agent type for mngr.

[OpenCode](https://github.com/sst/opencode) is an open-source terminal-based AI
coding agent. This plugin runs it as a first-class mngr agent: an interactive
TUI with RUNNING/WAITING lifecycle reporting, conversation resume across
stop/start, `mngr transcript` support, and per-agent config/credential
isolation.

## Usage

```bash
mngr create my-agent opencode
```

Pass arguments straight through to the `opencode` command with `--`:

```bash
mngr create my-agent opencode -- --model anthropic/claude-sonnet-4-5
```

`mngr stop` then `mngr start` resumes the agent's conversation; `mngr transcript
<agent>` prints the conversation; `mngr list` reports the agent as RUNNING while
it works and WAITING when it is idle. `mngr list` also reports a `waiting_reason`:
`PERMISSIONS` when blocked on an approval prompt, `END_OF_TURN` when idle.

One `opencode auth login` (in any agent) authenticates them all (the per-agent
`auth.json` is shared).

## Configuration

Define a custom variant in your mngr config (`mngr config edit`):

```toml
[agent_types.my_opencode]
parent_type = "opencode"
auto_allow_permissions = true

[agent_types.my_opencode.config_overrides]
model = "anthropic/claude-sonnet-4-5"
```

Then create agents with your custom type:

```bash
mngr create my-agent my_opencode
```

### Options

<!-- BEGIN GENERATED CONFIG TABLE (scripts/make_cli_docs.py) -->
| Option | Default | Meaning |
|---|---|---|
| `command` | `opencode` | Command to run the opencode agent. |
| `cli_args` | `()` | Extra arguments forwarded to the opencode attach (TUI) client. |
| `config_overrides` | `{}` | Key/value blob merged last into the per-agent opencode.json (e.g. model, the permission policy block). Example: {"model": "anthropic/claude-sonnet-4-5", "permission": {"bash": {"rm -rf *": "deny"}}}. |
| `sync_global_config` | `true` | Base the per-agent opencode.json on a copy of the user's ~/.config/opencode/opencode.json, or start from an empty base. |
| `symlink_auth` | `true` | Symlink the per-agent auth.json to the shared ~/.local/share/opencode/auth.json, so one login authenticates all agents. Set False for full isolation. |
| `auto_allow_permissions` | `false` | Auto-approve everything not explicitly denied (injects a wildcard allow into the opencode.json permission block). |
| `check_installation` | `true` | Check whether opencode is installed and install it if missing (if False, assume it is already present). |
| `version` | unset | Pin the opencode version to install (e.g., '0.4.10'). When set, installation runs the opencode installer with VERSION=<version> and provisioning verifies the installed opencode matches, erroring on a mismatch. When None (the default), installs the latest version. |
| `update_policy` | unset | How to handle opencode's startup auto-update. NEVER sets `"autoupdate": false` in the per-agent opencode.json so opencode does not update itself on launch; AUTO leaves auto-update enabled. ASK has no interactive flow for opencode and behaves like AUTO. When unset (the default), resolves to NEVER (auto-update disabled) -- set AUTO to leave opencode's auto-update enabled. An explicit `autoupdate` key in `config_overrides` always wins. |
| `emit_common_transcript` | `true` | Emit the common transcript that `mngr transcript` reads. |
| `preserve_on_destroy` | `true` | When destroying this agent, first copy its transcripts and resumable session store to <local_host_dir>/preserved/ so they survive. Set to False to discard them. |
<!-- END GENERATED CONFIG TABLE -->

## Choosing the model

The model is set through config (format: `provider/model`; list options with
`opencode models`). Three ways, highest precedence first:

1. **Per agent-type** — `config_overrides.model` on an `opencode` variant (the
   TOML example above). Applies to every agent of that type.
2. **On the `mngr create` command line** — mngr's generic per-invocation
   override:

   ```bash
   mngr create my-agent opencode -S agent_types.opencode.config_overrides.model=anthropic/claude-sonnet-4-5
   ```

3. **Globally** — the `"model"` in your `~/.config/opencode/opencode.json`,
   inherited when `sync_global_config` is `true` (the default).

Notes:

- If the model's provider isn't authenticated, OpenCode silently falls back to a
  free model — so if you set, say, `anthropic/claude-sonnet-4-5` but the agent
  shows a free "OpenCode Zen" model, run `opencode auth login` and
  create/restart the agent. (The free OpenCode-Zen models need no auth.)
- Passing the model after `--` (`mngr create ... opencode -- --model X`) does
  not work: `--` args go to the `opencode attach` client, which has no `--model`
  flag; the model is read by the `serve` process from `opencode.json`.

See the [mngr agent types documentation](https://github.com/imbue-ai/mngr/blob/main/libs/mngr/docs/concepts/agent_types.md)
for more details.
