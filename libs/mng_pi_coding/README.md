# mng-pi-coding

Plugin that registers the `pi-coding` agent type for mng.

[Pi](https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent) is a minimal terminal coding harness. This plugin lets you run it as an mng agent.

## Usage

```bash
mng create my-agent pi-coding
```

Pass arguments to the pi command with `--`:

```bash
mng create my-agent pi-coding -- --help
```

## Configuration

Define a custom variant in your mng config (`mng config edit`):

```toml
[agent_types.my_pi]
parent_type = "pi-coding"
cli_args = "--some-flag"
```

Then create agents with your custom type:

```bash
mng create my-agent my_pi
```

See the [mng agent types documentation](https://github.com/imbue-ai/mng/blob/main/libs/mng/docs/concepts/agent_types.md) for more details.
