# mng-opencode

Plugin that registers the `opencode` agent type for mng.

[OpenCode](https://github.com/sst/opencode) is an open-source terminal-based AI coding assistant. This plugin lets you run it as an mng agent.

## Usage

```bash
mng create my-agent opencode
```

Pass arguments to the opencode command with `--`:

```bash
mng create my-agent opencode -- --help
```

## Configuration

Define a custom variant in your mng config (`mng config edit`):

```toml
[agent_types.my_opencode]
parent_type = "opencode"
cli_args = "--some-flag"
```

Then create agents with your custom type:

```bash
mng create my-agent my_opencode
```

See the [mng agent types documentation](https://github.com/imbue-ai/mng/blob/main/libs/mng/docs/concepts/agent_types.md) for more details.
