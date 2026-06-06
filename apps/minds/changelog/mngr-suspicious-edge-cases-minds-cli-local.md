Hardened suspicious edge-case handling in the `minds` CLI:

- `minds env deploy` now rejects a present-but-partial `<prefix>/ovh` Vault
  entry (raising `VaultReadError`) instead of silently building empty OVH
  credentials and pushing a broken `ovh` Modal Secret. A fully-absent entry
  still proceeds with empty creds, as before.
- `minds run` now falls back to the activated env's real mngr host dir
  (`~/.{root_name}/mngr`) when `MNGR_HOST_DIR` is unset, instead of a
  hardcoded `~/.mngr` that disagreed with where the rest of the command
  looks. The explicit-override behavior is unchanged.
- Documented three intentionally-defensive branches (the `output_format`
  HUMAN default reached only on the unit-test path, the non-object-JSON
  branch of the generation-id check, and the `request_inbox is None` guard
  that only fires for test-built apps) so they read as deliberate rather
  than as silent fallbacks.
