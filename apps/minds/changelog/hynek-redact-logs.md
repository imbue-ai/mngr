Redact secrets from persisted logs.

The `mngr create` subprocess is now given a log-safe process `name` (via the new `concurrency_group` `name` parameter) with the latchkey gateway password and permissions-override JWT masked, so those secrets no longer leak into the JSONL log's `thread_name` field (nor any `ProcessError` message). The same command's "Running:" log line already masks them.

The `modal secret create` subprocess (used by `minds env` deploy tooling) is likewise named with its `KEY=VALUE` secret values masked, so raw Vault secrets no longer reach the thread name or error messages.

The Electron backend log (`minds.log`, uploaded with bug reports) now masks the `mngr forward` preauth cookie and the one-time login code before writing backend stdout events to disk.
