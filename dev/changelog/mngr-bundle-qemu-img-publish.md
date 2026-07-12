`scripts/lima_image/publish.py`'s `--uploader cloudflare-api` backend could never upload anything: its `exists()` presence check sent `HEAD` to the Cloudflare R2 object API, which answers `HEAD` with `405 Method Not Allowed`, so the publish aborted on the very first chunk. It now probes with `GET` (chunk bodies are small, so reading one back is cheap).

The object store also holds a single `httpx.Client` instead of calling the module-level helpers, which pools the connection across the thousands of small requests a store upload makes, and makes the store testable. `scripts/lima_image/publish_test.py` is new and pins the verb choice, so a regression to `HEAD` fails the suite rather than a release.

Found by publishing a real pre-baked image to a real R2 bucket.
