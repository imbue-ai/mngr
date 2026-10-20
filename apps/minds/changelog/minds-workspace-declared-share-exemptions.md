The Share tab now takes from the workspace how it lists each app. Each service registration carries `system` and `internal` (default-workspace-template's `system` and `internal` manifest keys), and they decide whatever the app is called:

- An ordinary app is listed as before.

- A system app (the chat, terminal, browser, and Getting Started) is listed after the apps, under a "System apps" group that starts closed. It starts open when a system app is selected or already has a grant, so an existing grant is never hidden. A system app can be granted like any other app. Before this change, the chat, terminal, and browser could only be shared as part of the whole workspace, and Getting Started was listed with the ordinary apps.

- An internal service (the terminal's `terminal-pty`, `owner-exec`, `vm-exec`) is never listed. This removes `terminal-pty`, and on a slice or VPS workspace `vm-exec`, both of which the tab used to offer.

Workspaces that predate the fields are sorted by a hardcoded list of names: chat, terminal, browser, and Getting Started are system apps, and `owner-exec`, `vm-exec`, and `terminal-pty` are internal. The list is marked for removal once no supported workspace predates the fields.

The workspace glossary gains a "system app" entry, and its grant entry now says a per-app grant admits system apps too, but never an internal service.

Tests: the `/ui/ws` tests now use a websocket client that also delivers server frames arriving together with the handshake response. The stock `simple_websocket` client left them unread, which intermittently failed `test_ws_connect_receives_hello_then_full_snapshot` under load (MIND-398).
