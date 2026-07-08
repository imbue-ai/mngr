# Plan: consistent handling of unauthenticated providers

## Refined prompt

I want to make mngr handle unauthenticated providers in a more consistent way.

Go gather all of the context for the mngr library, as well as all of the provider plugins, eg, lima, docker, vps, aws, gcp, azure, modal, vultr, and ovh (per instructions in CLAUDE.md).

Also try running "uv run mngr list". You'll see that the output is totally inconsistent -- each provider errors or fails in a different way.

What we *want* to happen is the following:
1. for most mngr commands that requires doing discovery, an unauthenticated provider should cause an immediate error
2. for "mngr list", unauthenticated providers should cause the command to fail with a non-zero exit code and a consistent set of correctly formatted errors, *but the rest of the command should run correctly*
(ie, for json or jsonl output, the errors should be properly represented there, and for human output, they should all simply be printed at the end with a consistent format)

While we're at it, let's check that none of the providers are getting "stuck" or taking a really long time when they're unauthenticated (GCP might have had some issue, but we should time each one via "uv run mngr list --provider <provider-name>" to see how long they run, and double check the code to ensure that they're all actually run in parallel)

* Standardize on `ProviderNotAuthorizedError` (made to inherit from `ProviderUnavailableError`) for missing/invalid credentials, raised consistently by aws, gcp, azure, modal, vultr, and ovh; keep plain `ProviderUnavailableError` for "reachable but down" (e.g. Docker daemon stopped); leave Lima as-is but format its errors consistently.
* The standardized error carries structured fields (`provider_name`, a short reason, a short remediation, and a reusable verbose `user_help_text`) so the list renderer can assemble output generically and identically across all providers.
* Requirement #1 ("most commands → immediate error") is satisfied by raising consistent error types/messages at provider construction; no new abort logic is needed for non-`list` commands.
* `mngr list` keeps its default `--on-error abort` (fails immediately on the first provider error); the "run the rest, show all errors" behavior applies under `--on-error continue`.
* Under `--on-error continue`, `mngr list` collects all provider failures, runs the rest correctly, prints one concise line per provider under an `Errors:` heading, and still exits non-zero.
* Streaming and batch human output both buffer provider errors into a single end-of-output `Errors:` block.
* Concise human error line format: `<provider>: <short reason> — <short remediation> (disable: mngr config set --scope user providers.<name>.is_enabled false)`.
* Exit code is the granular `EXIT_CODE_PROVIDER_INACCESSIBLE` (6) when every error is provider-inaccessible/auth (in both abort and continue modes); otherwise `1`.
* JSON/JSONL failures go in the structured `errors` channel (not ad-hoc stderr text), with a machine-readable `exception_type` plus a `help_text` field reusing the existing `user_help_text`.
* Vultr & OVH change from silently returning empty (exit 0) to raising the standard unauthenticated error; the bespoke `WARNING: Vultr API key...` print is removed. No downstream audit needed before flipping this.
* Any enabled-but-unauthenticated provider is an error (no "never configured" exemption) -- users opt into provider plugins at install time, so silently skipping them would be confusing.
* Add a configurable `credential_timeout_seconds` (float, default 10.0) per `[providers.<name>]` config section, bounding only credential/metadata (IMDS) resolution for aws/gcp/azure (hard timeout only, no warning threshold).
* Add eager Azure credential validation (currently only the subscription id is validated; `DefaultAzureCredential` authenticates lazily); disable IMDS probes where the SDK supports it.
* Modal gets the standardized error type only, not a `credential_timeout_seconds`.
* Do not suppress the GCP `UserWarning` for now.
* Verify providers actually discover in parallel and that none stall when unauthenticated.
* Test by unit-testing the shared error-collection/format/exit-code logic with a fake provider that raises `ProviderNotAuthorizedError`, plus acceptance tests asserting `mngr list` exit code and error format with a test provider; real-cloud auth is left untested in CI.
* Add changelog entries for mngr core and every touched provider plugin.

## Overview

- Today every backend reports an unauthenticated state differently: aws/gcp/azure raise `ProviderUnavailableError` (exit 1, verbose multi-line help), Vultr logs a bespoke `WARNING` and returns empty (exit 0), OVH silently returns empty (exit 0), Lima degrades to local state, and in `mngr list`'s default abort mode only the first failing provider's message ever surfaces (non-deterministic).
- We will make "unauthenticated" a single, consistent concept: a `ProviderNotAuthorizedError` (subclass of `ProviderUnavailableError`) carrying structured fields, raised the same way by all credentialed backends (aws, gcp, azure, modal, vultr, ovh).
- `mngr list` becomes the one read command that tolerates these failures: in `--on-error continue` it runs the rest of discovery, prints all provider errors in one consistent block (or the structured `errors` channel for json/jsonl), and exits with a granular non-zero code. All other discovery commands keep failing immediately (already true via construction-time raises).
- Unauthenticated providers must fail fast: a configurable per-provider `credential_timeout_seconds` (default 10s) bounds credential/metadata resolution for aws/gcp/azure, Azure validates its credential eagerly, and IMDS probes are disabled where possible -- so an opted-in but unconfigured cloud provider errors quickly instead of hanging.
- Scope is the error contract, the `mngr list` formatting/exit-code path, and per-backend credential handling; the parallel discovery fan-out (thread-pool, `max_workers=32`) already exists and only needs verification.

## Expected behavior

- `mngr <command>` that targets a single unauthenticated provider (e.g. `create`, `exec`, `destroy`) fails immediately with one consistent `ProviderNotAuthorizedError` message and remediation.
- `mngr list` (default `--on-error abort`): aborts on the first provider failure, exiting `6` when that failure is auth/inaccessibility (was `1`).
- `mngr list --on-error continue`: prints all reachable agents normally, then prints an `Errors:` block at the very end with one concise line per failed provider, and exits `6` (or `1` if any non-auth/non-inaccessible error is mixed in).
- Concise human line reads, e.g.: `azure: not authenticated — run 'az login' (disable: mngr config set --scope user providers.azure.is_enabled false)`.
- `mngr list --format json`: provider failures appear in the `errors` array (each with `provider_name`, `exception_type`, `message`, and `help_text`), never as ad-hoc `Error: Discovery failed...` text on stderr; the `agents` array still contains all reachable agents.
- `mngr list --format jsonl`: each provider failure is emitted as a structured error event line; agent lines are still streamed for reachable providers.
- Vultr and OVH, when unauthenticated, now appear as errors (exit non-zero) instead of silently reporting zero agents; the `WARNING: Vultr API key not configured...` line is gone.
- An enabled cloud provider with no credentials at all is treated identically to one with invalid credentials: both are errors (no silent skip).
- Unauthenticated aws/gcp/azure return within their `credential_timeout_seconds` (default 10s) rather than hanging on metadata-server probes; Azure surfaces the auth failure at discovery time rather than lazily on a later call.
- All providers continue to be discovered in parallel; one slow/failing provider does not serialize or block the others.
- Lima, local, and Docker behavior is unchanged except that any errors they surface in `mngr list` are formatted through the same consistent path; Docker-daemon-down remains a plain `ProviderUnavailableError`.

## Changes

- Add `ProviderNotAuthorizedError` as a subclass of `ProviderUnavailableError` in mngr core `errors.py`, carrying structured fields (`provider_name`, short reason, short remediation, verbose `user_help_text`) used to render both the concise human line and the structured json `help_text`.
- Update the `mngr list` error path so that, in `continue` mode, provider failures are collected and rendered in a single consistent `Errors:` block at the end of both the streaming and batch human output paths.
- Make `mngr list` choose its exit code by error category: `EXIT_CODE_PROVIDER_INACCESSIBLE` (6) when all errors are provider-inaccessible/auth, else `1`; apply this in both abort and continue modes.
- Ensure json/jsonl output routes provider failures through the structured `errors` channel, adding a `help_text` field (sourced from the error's `user_help_text`) and a machine-readable `exception_type`, instead of emitting ad-hoc stderr error text.
- Convert aws, gcp, azure, and modal construction/discovery auth failures to raise the standardized `ProviderNotAuthorizedError` (with their existing remediation text mapped into the structured fields).
- Change Vultr and OVH to raise `ProviderNotAuthorizedError` when unauthenticated instead of returning empty; remove the bespoke Vultr `WARNING` print and the OVH silent-empty branch.
- Add a `credential_timeout_seconds` config field (float, default 10.0) to the aws, gcp, and azure provider config sections, and apply it as a hard timeout bounding only credential/metadata (IMDS) resolution.
- Add eager Azure credential validation at construction (validate the credential, not just the subscription id) and disable IMDS/metadata probes for aws/gcp/azure where the SDK supports it (e.g. `AWS_EC2_METADATA_DISABLED`).
- Leave Lima, local, and Docker auth/availability semantics as they are, but ensure their surfaced errors flow through the consistent `mngr list` formatting path.
- Do not change the GCP `UserWarning` behavior.
- Verify (and document via timings) that discovery runs in parallel and that unauthenticated providers return within the timeout.
- Add unit tests for the shared error-collection/formatting/exit-code logic using a fake provider that raises `ProviderNotAuthorizedError`, and acceptance tests asserting `mngr list` exit codes and error formatting with a test provider.
- Add changelog entries for mngr core and each touched provider plugin (mngr_aws, mngr_gcp, mngr_azure, mngr_modal, mngr_vultr, mngr_ovh).
