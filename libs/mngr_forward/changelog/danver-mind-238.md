Fixed the root cause of the flaky `test_tampered_session_cookie_rejected` (MIND-238), which failed about one run in sixteen.

The test walks every character of a valid session cookie, replaces it, and asserts the altered cookie is rejected. Its replacement rule (`"A"` -> `"B"`, else -> `"A"`) was not always a real tamper. itsdangerous renders its HMAC signature (and the timestamp) as base64url, and the final character of such a segment carries padding bits that decoding discards. A canonical cookie leaves those padding bits zero, so the last signature character is `"A"` about one time in sixteen -- and flipping `"A"` -> `"B"` there changes only a padding bit. The altered cookie decodes to the same bytes, so it is the same credential, and the proxy correctly accepted it, tripping the assertion.

The replacement now maps `"A"`-`"D"` (which share their high four bits) to `"Q"` and everything else to `"A"`, so every single-byte alteration changes the significant high bits at every position, including the last character of each segment. Brute force over 500 freshly minted cookies dropped from 32 wrongly-accepted alterations to 0.

Test-only: the proxy's cookie verification is unchanged, and the test's `witnesses` markers (`authentication.tampered-cookie-rejected`, `sessions-unforgeable`) still hold -- the fix strengthens what it witnesses rather than weakening it.
