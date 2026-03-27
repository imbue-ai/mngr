# mngr-opencode

Plugin that registers the `opencode` agent type for mngr.

[OpenCode](https://github.com/sst/opencode) is an open-source terminal-based AI coding assistant. This plugin lets you run it as an mngr agent.

## Usage

```bash
mngr create my-agent opencode
```

Pass arguments to the opencode command with `--`:

```bash
mngr create my-agent opencode -- --help
```

## Configuration

Define a custom variant in your mngr config (`mngr config edit`):

```toml
[agent_types.my_opencode]
parent_type = "opencode"
cli_args = "--some-flag"
```

Then create agents with your custom type:

```bash
mngr create my-agent my_opencode
```

See the [mngr agent types documentation](https://github.com/imbue-ai/mngr/blob/main/libs/mngr/docs/concepts/agent_types.md) for more details.
