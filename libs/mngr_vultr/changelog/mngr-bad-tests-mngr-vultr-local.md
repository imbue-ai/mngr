Improved the Vultr provider test suite. The API client now accepts an
injectable HTTP transport so its unit tests verify the requests it builds
(URLs, base64-encoded user_data, body shape) instead of only parsing canned
responses, with no reliance on `unittest.mock`. Added coverage for backend
provider construction and tag-based VPS discovery, and hardened the release
tests to use unique agent names and to confirm a destroyed VPS is actually
gone rather than sleeping a fixed interval.

No user-facing behavior changes.
