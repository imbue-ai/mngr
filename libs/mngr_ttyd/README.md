# mngr-ttyd

Web terminal plugin for mngr.

A plugin for [mngr](https://github.com/imbue-ai/mngr) that automatically launches a [ttyd](https://github.com/tsl0922/ttyd) web terminal server alongside each agent, giving you browser-based terminal access to the agent.

## Requirements

- `ttyd` must be installed on the host machine (the plugin installs `ttyd` 1.7.7 automatically if missing)

## Clipboard support

Released `ttyd` (1.7.7) ships a web client whose bundled xterm.js has no OSC 52
handler, so copying text inside a tmux session running in the browser terminal
never reaches the system clipboard. This plugin ships its own web client
(`resources/ttyd_index.html.gz`, served to the stock binary via `ttyd -I`) that
adds OSC 52 support, so a plain mouse-drag copy inside tmux lands on the system
clipboard while `mouse on` keeps wheel scroll and in-app mouse working.

The client is built from `ttyd`'s `main` branch (which adds
`@xterm/addon-clipboard`) with a small patch so it also accepts the empty OSC 52
selection target that tmux emits. To rebuild it, run
`scripts/build_patched_ttyd_client.sh` (the patch lives in
`scripts/ttyd_clipboard_provider.patch`).

OSC 52 clipboard writes require a secure browser context (HTTPS or `localhost`)
and a focused tab.
