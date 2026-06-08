# Harden suspicious edge-case handling under `mngr/api`

A cleanup pass over over-defensive error handling in the `mngr` API layer. User-visible effects:

- `mngr gc` no longer aborts the whole pass when a single log/cache filesystem operation fails (the per-entry handlers now catch the `OSError` they actually raise), and it warns instead of silently leaving orphaned resources when a provider backend is unreachable.
- `mngr gc` distinguishes "kept because of genuinely unpushed branches" from "kept because branches could not be listed", so the user-facing keep message is accurate; and it no longer records a directory whose mtime could not be read as freshly created.
- `mngr cleanup` reports agents that were already absent from their host as a distinct outcome rather than counting them as successful destroys (a false success).
- `mngr message` no longer silently drops agents on a host whose provider is missing or that are in a non-messageable lifecycle state (`DONE`/`RUNNING_UNKNOWN_AGENT_TYPE`); these are now recorded as failures. Only `RUNNING`/`WAITING`/`REPLACED`/`UNKNOWN` agents are messaged.
- `mngr connect` raises (instead of returning as if cleanly disconnected) when an SSH connection fails on every retry.
- `mngr rsync` warns that transfer counts are unknown when rsync's `--stats` block can't be parsed, instead of reporting a misleading "0 files, 0 bytes".
- Discovery/event handling is more robust: a host with no determinable provider is skipped rather than poisoning the event stream with a fake `"unknown"` provider; missing `event_id`/`state` fields in event/history records are surfaced (raised or warned) rather than silently backfilled or dropped; and the discovery tail loop no longer retries a real logic bug every second.

Internal hardening: built-in `RuntimeError`/`assert`/`raise` replaced with the `MngrError` hierarchy and `assert_never` exhaustiveness checks; several broad `except` clauses narrowed to the specific raising statements; and a few in-band sentinels (`0`-for-"no snapshot", `datetime.now()`-for-"unknown") replaced with honest `| None` types.
