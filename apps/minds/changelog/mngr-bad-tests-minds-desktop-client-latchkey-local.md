Hardened and de-duplicated the latchkey desktop-client tests after a bad-test review:

- Fixed two gateway-client connect-error "self-heal" tests that could not fail: they now assert the specific `LatchkeyGatewayClientNotInitializedError` on the post-invalidation call and that the transport was reached exactly once, so a regression that stopped clearing the cached base URL would actually be caught.
- Stopped the permissions-merge tests from pinning the in-process fake's reimplementation: the replace test now asserts the handler's real forwarding contract via `set_calls`, the trivial on-disk equality check was dropped (the gateway-call contract is covered by a dedicated test), and the schema-preservation case was moved to authoritative coverage against the real gateway extension (in `libs/mngr_latchkey`).
- Removed a dead-import `_ = (...)` block that kept unused imports alive past the linter.
- Strengthened the "does not raise on failure" message-sender test to also assert the send was actually attempted before the failure was swallowed.
- Replaced a brittle `assert "<script>" in body` render assertion with one tied to user-facing behavior (the dialog's form posts to the event's grant route).
- Moved the full-app FastAPI dispatcher tests (which stand up the desktop client via `create_desktop_client` and drive HTTP routing through a `TestClient`) into a dedicated integration file `test_latchkey_handlers.py`, leaving only fast isolated unit tests in the `*_test.py` files. Shared helpers were extracted into a new `handlers/testing.py` module to eliminate duplication.

No product behavior changed; these are test-only changes.
