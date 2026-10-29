Phase 3C of the user-profile-state spec: keybindings become the first account-keyed setting a workspace consumes and the only one it may change.

- `keybindings` is a declared account-keyed setting (`desktop_client/settings/schema.py`) with the workspace-per-user override kind, carried on `AccountSettings` as app name -> action id -> chord list and resolved through the new `resolve_keybindings` wrapper.

- A new chord grammar module (`desktop_client/settings/keybindings.py`) validates the canonical chord spelling (lowercase tokens joined with `+`, modifiers in the fixed ctrl/alt/shift/meta order, named keys from the lowercased `KeyboardEvent.key` vocabulary); the vocabulary is pinned cross-repo through `workspace_profile_contract.json`.

- The workspace profile snapshot now carries the owning account's keybindings: `WorkspaceProfileWriter` resolves them from the account settings store, and the contract fixture's `setting_keys` gains `"keybindings"` (the template's reader pins against it).

- New cross-workspace verb route `PUT /api/v1/workspaces/<agent_id>/account-settings/keybindings`, gated by the new `minds-workspaces-account-settings` latchkey verb: it validates the chord grammar, resolves the workspace's owning account from its association (409 for an unassociated workspace), applies the body to that account's settings replica, marks the workspace's snapshot stale, kicks the sync scheduler, and answers the applied value.
