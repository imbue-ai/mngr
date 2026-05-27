# Fix a signing-key generation race that intermittently logged users out

`FileAuthStore.get_signing_key` generated the cookie signing key lazily
on first access without any synchronization. FastAPI dispatches sync
route handlers on a threadpool, so on a fresh data directory the desktop
client's startup burst -- `/authenticate` plus the `/` redirect target,
`/_chrome`, and `/welcome`, each of which checks authentication -- could
all reach key generation concurrently. Two interleavings both broke auth:

- A reader saw the just-created key file as momentarily empty (the old
  code did a non-atomic `write_text`) and raised `SigningKeyError`, so
  `/authenticate` returned 500 and no session cookie was set.
- Two threads each generated a *different* key and raced to write it;
  the last writer won and silently invalidated the cookie that had just
  been signed with the earlier key, so the next request's
  `verify_session_cookie` failed and the user appeared logged out.

Either way the subsequent page load came back unauthenticated. This was
the dominant cause of flaky failures in the `test-docker-electron` CI job
(`test_create_local_docker_workspace_via_electron` timing out on the
`#create-form` selector because `GET /create` returned 403).

`get_signing_key` now reads the existing key on the fast path and, when
it must generate one, serializes generation behind a per-store lock with
a double-checked re-read and writes the key via `atomic_write` so a
concurrent reader never observes a partial file. Concurrent first-time
callers now always converge on a single persisted key.
