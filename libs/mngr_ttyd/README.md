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

## Refit on reconnect

The stock client disposes all of its listeners (including its only refit
trigger, window resize -> `fitAddon.fit`) on every websocket close and only
re-registers them once the reconnect completes, so a resize that happens during
a disconnect window is lost: the reconnect handshake then sizes the new PTY to
the stale columns/rows and nothing re-measures afterwards. The vendored client
carries a second patch (`scripts/ttyd_reconnect_refit.patch`, applied by the
same build script) that re-measures on reconnect before sending the size
handshake.
