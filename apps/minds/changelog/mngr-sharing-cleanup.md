Sharing identity cleanup (design: `specs/share-identity-and-presence/spec.md`):

- In-workspace services learn who is asking from one `X-Imbue-Identity` header on every request, over the relay and over the local forward alike. The desktop no longer writes the owner's email into the workspace (`data/.state/share/owner_email` is gone); instead it maintains `forward_identity.json` (the owner's account record per shared workspace) and hands it to `mngr forward`, so the owner's local requests to a shared workspace carry their id, email, name, and profile picture.

- Grants are keyed by account: adding an address in the Share tab resolves it to an account (`POST /ui/api/users/resolve`) and stores the grant under `users`; an address with no account is stored as an invite the workspace gateway upgrades on first visit. User entries render with the person's profile picture, name, and email, and the grants document round-trips `users` so upgrades are never lost. Saving a share also mirrors its user-id grants to the connector's grantee index and the owner's contacts (best effort).

- The desktop's grants writes into the workspace run under the same file lock the gateway takes, so the two never tear the document.

- Account identity (display name, profile picture) is refreshed from the connector every five minutes, and the forward's identity file is reconciled against each account's active shares on the same schedule (and right after a sign-in or sign-out).
