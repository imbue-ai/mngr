The remote (VPS) gateway's on-host locations are now public constants: `REMOTE_LATCHKEY_DIR_NAME`, `REMOTE_GATEWAY_LOG_FILENAME`, and `REMOTE_TUNNEL_LOG_FILENAME` in `remote_gateway`. Minds' bug-report diagnostics tail the gateway logs at these paths over `mngr exec --outer`, so the paths are one shared source of truth instead of duplicated strings.

Fixed a stale docstring that pointed the forward supervisor's structured log at `<plugin_data_dir>/forward_logs/events.jsonl`; it lives at `<plugin_data_dir>/events.jsonl`.
