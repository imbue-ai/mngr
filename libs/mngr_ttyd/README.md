# mngr-ttyd

Web terminal plugin for mngr.

A plugin for [mngr](https://github.com/imbue-ai/mngr) that automatically launches a [ttyd](https://github.com/tsl0922/ttyd) web terminal server alongside each agent, giving you browser-based terminal access to the agent.

## Requirements

`ttyd` must be on the host's PATH. When it is missing, the plugin installs 1.7.7 from the
upstream GitHub release, but only onto a host where that can actually work:

- **Linux** with a writable `/usr/local/bin`, or with passwordless `sudo`: installed automatically.
- **macOS**: not installed. The upstream release ships Linux binaries only, so there is nothing
  to fetch. Run `brew install ttyd` yourself.
- **Anywhere `/usr/local/bin` is unwritable and `sudo` wants a password**: not installed.

When the plugin cannot install `ttyd`, it says so and the agent comes up without its web
terminal; everything else about the agent is unaffected. The check costs a few milliseconds,
so an agent on a host that will never be installable does not pay for a download on every
`mngr create`.

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
