# mng-ttyd

Web terminal plugin for mng.

A plugin for [mng](https://github.com/imbue-ai/mng) that automatically launches a [ttyd](https://github.com/tsl0922/ttyd) web terminal server alongside agents. The terminal is accessible through the mind's forwarding server.

## How it works

When an agent is created, this plugin adds an extra tmux window running ttyd on a random port. The ttyd process writes a server registration event so the forwarding server can discover and proxy the terminal.

## Requirements

- `ttyd` must be installed on the host machine
