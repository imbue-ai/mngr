Phase 3A of `specs/user-profile-state/spec.md`: account-keyed settings follow the account. The update window, the auto-update toggle and its one-time offer's answer, and the master notifications toggle move out of `config.toml` into one record per account id (the device's `local` included); the signed-in accounts' records sync through the connector's account documents store as the `settings` document, pulled and pushed on every sync pass -- so a preference set on one computer reaches every computer the account is signed in on.

- A generic `AccountDocumentStore` (replica files, dirty-wins pulls, CAS pushes with one local-wins rebase, tombstones, the too-new gate) is factored out of the connection store; the connection store and the new settings store are its two clients.

- The settings routes write the per-account replica and kick the scheduler; the settings overview takes the window's account as a query parameter; the update machinery and the notification dispatch gate resolve each workspace's or notification's owning account; the updates frame carries each account's own window and toggle.

- A one-time migration seeds the records from `config.toml`'s values (an account's existing server document wins on first pull) and deletes the moved keys.
