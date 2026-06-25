Hardened suspicious edge-case handling in the pi-coding plugin:

- The best-effort credential check (`_has_api_credentials_available`) no longer aborts
  agent creation when `~/.pi/agent/auth.json` is corrupt or unreadable. A malformed
  `auth.json` is now caught and treated as "no credentials detected" (emitting a warning),
  matching the behavior of the claude plugin, instead of letting a `json.JSONDecodeError`
  propagate out of provisioning.
- Documented why local config-dir symlink failures are non-fatal (warn-and-continue) while
  the surrounding `mkdir` is fatal: pi credential/settings sync is advisory.
- Made local resource-dir sync consistent with the remote path by skipping non-directory
  entries (`skills`/`prompts`/`extensions`/`themes`).
