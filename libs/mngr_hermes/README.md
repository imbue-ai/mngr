# imbue-mngr-hermes

Plugin that registers the `hermes` agent type for mngr.

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is an interactive AI agent framework by Nous Research with its own TUI. This plugin lets you run it as an mngr agent with isolated per-agent configuration.

## Usage

```bash
mngr create my-agent hermes
```

Pass arguments to the hermes command with `--`:

```bash
mngr create my-agent hermes -- -m anthropic/claude-sonnet-4 -t code,web
```

## Configuration

Define a custom variant in your mngr config (`mngr config edit`):

```toml
[agent_types.my_hermes]
parent_type = "hermes"
cli_args = ["-m", "anthropic/claude-sonnet-4"]
```

Then create agents with your custom type:

```bash
mngr create my-agent my_hermes
```

## How it works

Each hermes agent gets an isolated `HERMES_HOME` directory inside its agent state directory. During provisioning, the plugin seeds this directory from your `~/.hermes` config (config.yaml, .env, auth.json, SOUL.md, memories/, skills/, home/). Runtime state (sessions, logs, plans, etc.) starts fresh for each agent.

See the [mngr agent types documentation](https://github.com/imbue-ai/mngr/blob/main/libs/mngr/docs/concepts/agent_types.md) for more details.
