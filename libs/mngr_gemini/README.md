# imbue-mngr-gemini

Plugin that registers the `gemini` agent type for mngr.

[Gemini CLI](https://geminicli.com) is Google's terminal-based AI coding assistant. This plugin lets you run it as an mngr agent.

## Usage

```bash
mngr create my-agent gemini
```

Pass arguments to the gemini command with `--`:

```bash
mngr create my-agent gemini -- --help
```

## Configuration

Define a custom variant in your mngr config (`mngr config edit`):

```toml
[agent_types.my_gemini]
parent_type = "gemini"
cli_args = "--some-flag"
```

Then create agents with your custom type:

```bash
mngr create my-agent my_gemini
```

See the [mngr agent types documentation](https://github.com/imbue-ai/mngr/blob/main/libs/mngr/docs/concepts/agent_types.md) for more details.
