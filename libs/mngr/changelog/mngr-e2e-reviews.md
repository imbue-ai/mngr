Addressed Josh's review feedback on PR #1937:

- `mngr gc --provider <name>` now exits non-zero when an explicitly-named provider was skipped because it was empty (e.g. a fresh Modal per-user environment) or unavailable. The other selected providers still run to completion; the skipped providers are reported in the summary so the user can see what was not gc'd. The automatic post-destroy gc path (which always passes `--all-providers` internally and tolerates skips) is unaffected.

- Removed the `-a` / `--all` / `--all-agents` flag from `mngr message` (alias `mngr msg`). The tutorial and CLI examples now use the explicit `mngr list --ids | mngr msg -` pattern. Users who relied on `-a` should switch to piping ids from `mngr list` (optionally with `--include` / `--exclude` to scope the broadcast).

- Dropped `--no-ensure-clean` from the agent-type e2e tutorial tests. The e2e fixture now gitignores the per-test project config directory (where `mngr config set` writes its files), so the working tree stays clean and the flag is no longer needed. The `@pytest.mark.rsync` markers on those tests (whose only purpose was to satisfy the resource guard for the rsync path that `--no-ensure-clean` happened to trigger) are removed alongside.

- Removed a stale duplicate `type = "claude"` line in the e2e fixture's seeded `settings.local.toml` that was causing every release-tier e2e/tutorial test to fail with "Cannot overwrite a value".
