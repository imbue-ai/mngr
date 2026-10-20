Hide latchkey's built-in `notion` service from agents.

The gateway now merges a `settings.hideBuiltinServices` list (currently just `notion`) into `LATCHKEY_DIRECTORY/config.json` before each spawn, so agents no longer see the built-in `notion` service alongside the separate `notion-mcp` integration (which was a source of confusion). Existing config content is preserved via a read-merge-write.

The same hidden set is applied to VPS-resident gateways provisioned by `provision_remote_gateway`, so agents talking to a per-VPS gateway see the same service list as those talking to the desktop gateway.
