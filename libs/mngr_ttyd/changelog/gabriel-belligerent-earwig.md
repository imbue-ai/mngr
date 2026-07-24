Fix the vendored ttyd web client leaving the terminal at a stale size after a websocket reconnect.

The client disposes all its listeners (including its only refit trigger, window resize -> fitAddon.fit) on every socket close, so any resize that happens during a disconnect window is lost; the reconnect handshake then resizes the new PTY to the stale columns/rows and nothing re-measures afterwards. This is the mechanism behind the minds desktop client's "terminal tab does not reflow after dragging the split sash until you click into it and drag again" bug (terminal websockets there traverse the latchkey forward and drop intermittently).

The client now re-measures (`fitAddon.fit()`) on reconnect, before sending the size handshake, via a new source patch (`scripts/ttyd_reconnect_refit.patch`) applied by `scripts/build_patched_ttyd_client.sh`; `resources/ttyd_index.html.gz` is rebuilt with it.
