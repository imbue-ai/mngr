# Offload pin bump to v0.9.6

Bumped the offload version baked into `libs/mngr/imbue/mngr/resources/Dockerfile`
from `0.9.5` to `0.9.6` to keep the in-image offload binary in lockstep
with the CI pin. v0.9.6's headline feature is the new
`offload run --override-image-id <ID>` CLI flag (Modal provider only),
which lets a test run skip offload's image-setup pipeline entirely and
boot from a pre-built Modal image. See
https://github.com/imbue-ai/offload/releases/tag/v0.9.6 for the full
release notes.
