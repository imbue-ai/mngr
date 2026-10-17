This branch carries the sharing identity cleanup merged from `mngr/sharing-cleanup`; that branch's own entry (`mngr-sharing-cleanup.md`) describes the changes in full. No client or CLI changes are needed for the shared-desktop work.

- New `PublicProfile` wire model, client method `get_public_profile(user_id)`, and CLI `mngr imbue_cloud users profile <user_id>` (needs no account: the connector serves the profile unauthenticated and answers nulls, not an error, for an id without a profile).

- New `SyncRecordsListing` wire model and client method `list_sync_records_with_shares` (records plus the `shared_agent_ids` of the account's active shares; empty against an older connector). `list_sync_records` is unchanged. `mngr imbue_cloud sync records pull` now prints `{"records": [...], "shared_agent_ids": [...]}`.

- `AuthSession`, `AccountInfo`, `UserIdentity`, and `PublicProfile` carry `profile_picture_url` instead of `avatar_url`, matching the connector's rename of the user's avatar to "profile picture"; `mngr imbue_cloud auth list`, `users show`, `users profile`, and `users resolve` print the new key, and the persisted session file stores it under `profile_picture_url`. No compatibility with the old key: it only ever existed on this branch.
