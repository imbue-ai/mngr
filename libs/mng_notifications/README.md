# mng-notifications

Desktop notifications when agents need your attention.

A plugin for [mng](https://github.com/imbue-ai/mng) that adds the `mng watch` command. It monitors the event stream from `mng observe` and sends a native desktop notification whenever an agent transitions from RUNNING to WAITING.

## Requirements

- macOS: `brew install terminal-notifier`
- Linux: `notify-send` (usually part of `libnotify`)

## Usage

```bash
mng watch
```

When an agent finishes working and waits for input, you get a notification. On macOS, clicking the notification opens a terminal tab connected to that agent.

If `mng observe` is not already running, `mng watch` starts it automatically in the background and stops it on exit.

## Configuration

Add to your mng settings file (e.g. `~/.mng/settings.toml`):

```toml
[plugins.notifications]
terminal_app = "iTerm"
```

Supported terminal apps: `iTerm`, `Terminal`, `WezTerm`, `Kitty`. For iTerm, clicking a notification finds an existing tab already connected to the agent (by matching tmux session TTYs) or opens a new one.

For other terminals, use a custom command:

```toml
[plugins.notifications]
custom_terminal_command = "my-terminal -e mng connect $MNG_AGENT_NAME"
```

`$MNG_AGENT_NAME` is set in the environment to the agent's name.

For plain notifications without click-to-connect (useful on Linux where `notify-send` does not support click actions):

```toml
[plugins.notifications]
notification_only = true
```
