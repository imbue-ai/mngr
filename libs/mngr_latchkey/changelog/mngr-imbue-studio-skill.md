- `mngr latchkey forward` is no longer spawned detached by the Imbue Studio desktop client: it is a coupled child, stopped on the app's shutdown, and the new `--parent-pid <pid>` flag makes it exit on its own when that process is gone, so the gateway, its `mngr observe` child, and the reverse tunnels never outlive the app that started them. `LatchkeyForwardSupervisor` gains a `parent_pid` field and `stop()` is now part of the embedder's teardown.

- `spawn_detached_mngr_latchkey_forward` and `spawn_detached_latchkey_ensure_browser` are renamed `spawn_mngr_latchkey_forward` and `spawn_latchkey_ensure_browser`; neither starts a new session any more.

- The `mngr latchkey forward` help text no longer says `Latchkey.initialize()` adopts or discards a pre-existing detached gateway record; it version-checks the binary and writes the bundled service registrations into latchkey's `config.json`, and nothing adopts a gateway.
