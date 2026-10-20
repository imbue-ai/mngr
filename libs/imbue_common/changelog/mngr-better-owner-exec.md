- Added `owner_exec_client.py`: a signed client for the owner-exec service
  (SSH-equivalent authority over a workspace over HTTP). It signs requests per
  the RFC 9421/9530 strict profile with an Ed25519 key and verifies every
  response (and the `/run` stream trailer) against the endpoint's pinned SSH
  host key, failing closed on a bad signature. Cross-checked against the shared
  vectors published by the `imbue-ai/owner-exec` repo.

- The shared `PREVENT_EXEC` ratchet regex no longer misfires on hyphenated
  prose like `owner-exec (vm role)` inside string literals.
