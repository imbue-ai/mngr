Fixed a flaky test in the desktop client's browser-authorization suite.

`test_tampered_session_cookie_is_unauthenticated` altered the last character of a valid session cookie and asserted the result is rejected. But the cookie's signature is a 20-byte HMAC-SHA1 rendered as 27 base64url characters, so that final character carries only 4 significant bits: several distinct characters there decode to the identical signature. Roughly 6-10% of runs therefore produced a "tampered" cookie that was in fact byte-identical after decoding, authenticated normally, and failed the assertion with `assert '/' == '/login'`.

The test now alters the first character of the signature segment, where all 6 bits are significant, and asserts up front that the mutated cookie actually fails `verify_session_cookie` -- so any future mutation that decodes to the same bytes fails at the mutation rather than as a confusing redirect mismatch.

Test-only: the tampered-cookie rejection under test was always correct, and no product behavior changes.
