Update offload to 0.9.1. The Dockerfile now installs offload from crates.io
(versioned release) instead of a pinned pre-merge git rev, since the
`apply-diff` subcommand shipped in 0.9.1.
