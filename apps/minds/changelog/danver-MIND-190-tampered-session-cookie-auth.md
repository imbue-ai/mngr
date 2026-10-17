Hardened the desktop-client browser-authorization tests that check a tampered session cookie is rejected, removing a lingering source of CI flakiness (MIND-190).

Two tests each hand-built a "tampered" cookie by flipping one base64 character, each with its own delicate reasoning about which character was safe to touch. A session cookie is an itsdangerous token whose signature is an HMAC over the encoded signed-content string, and the signature is the only segment a verifier base64-decodes before comparing -- so a flip in the signature's base64 tail can be absorbed by its spare bits and still authenticate (~7% of cookies), which is what made `test_tampered_session_cookie_is_unauthenticated` fail intermittently with `assert '/' == '/login'`.

Both tests now build the tampered cookie through a single shared helper that alters the signed content instead of the signature. Any change there changes the HMAC input and is rejected with certainty, whatever the payload, so the tamper is provably invalidating by construction rather than by a per-test argument. The now-redundant guard assertions the earlier fix added are gone.

Test-only: the tampered-cookie rejection under test was always correct, and no product behavior changes.
