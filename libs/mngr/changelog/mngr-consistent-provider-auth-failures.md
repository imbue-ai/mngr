Handle unauthenticated providers consistently across `mngr list` and other commands.

Previously every provider reported a missing-credentials state differently: AWS/GCP/Azure raised a verbose error and exited 1, Vultr printed an ad-hoc `WARNING` and exited 0, OVH silently reported zero agents, and a full `mngr list` (default `--on-error abort`) surfaced only whichever provider happened to fail first.

Now:

- A new shared `ProviderNotAuthorizedError` (a subclass of `ProviderUnavailableError`) represents "enabled but unauthenticated" for every provider, carrying structured `short_reason` / `short_remediation` fields plus verbose help text.

- `mngr list --on-error continue` runs the rest of the listing, then reports every failing provider in one consistent block: a single glanceable line per provider on stderr for human output (`<provider>: <reason> — <remediation> (disable: mngr config set ...)`), and structured entries in the `errors` array (with `exception_type`, `help_text`, and an `is_provider_inaccessible` flag) for `--format json` / `jsonl`. The default `--on-error abort` still fails immediately on the first provider error.

- `mngr list` now exits with the granular provider-inaccessible code (6) when every error is a provider that could not be reached or authenticated, and 1 otherwise -- in both abort and continue modes.

- The OVH backend was missing from the internal remote-backends list, so it loaded (and silently no-op'd) in environments where the other cloud backends were correctly skipped; it is now treated like AWS/GCP/Azure/Vultr.
