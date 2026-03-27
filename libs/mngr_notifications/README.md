# mngr-notifications

Desktop notifications when agents need your attention.

A plugin for [mngr](https://github.com/imbue-ai/mngr) that adds the `mngr notify` command. It monitors the event stream from `mngr observe` and sends a native desktop notification whenever an agent transitions from RUNNING to WAITING.

## Requirements

- macOS: `brew install terminal-notifier`
- Linux: `notify-send` (usually part of `libnotify`)

## Usage

```bash
mngr notify
```

When an agent finishes working and waits for input, you get a notification. On macOS, clicking the notification opens a terminal tab connected to that agent.

If `mngr observe` is not already running, `mngr notify` starts it automatically in the background and stops it on exit.

## Configuration

Add to your mngr settings file (e.g. `~/.mngr/settings.toml`):

```toml
[plugins.notifications]
terminal_app = "iTerm"
```

Supported terminal apps: `iTerm`, `Terminal`, `WezTerm`, `Kitty`. For iTerm, clicking a notification finds an existing tab already connected to the agent (by matching tmux session TTYs) or opens a new one.

For other terminals, use a custom command:

```toml
[plugins.notifications]
custom_terminal_command = "my-terminal -e mngr connect $MNGR_AGENT_NAME"
```

`$MNGR_AGENT_NAME` is set in the environment to the agent's name.

For plain notifications without click-to-connect (useful on Linux where `notify-send` does not support click actions):

```toml
[plugins.notifications]
notification_only = true
```
