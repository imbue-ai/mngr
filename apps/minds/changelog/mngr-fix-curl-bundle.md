- The bundled Chrome-impersonating curl (which is how agents clear Cloudflare-style TLS fingerprinting on third-party services) is now datalib v0.26.0, was v0.25.0. On Linux the statically linked musl build is bundled instead of the glibc one, so it runs on any distro rather than only on those whose glibc is at least as new as datalib's build host.

- `pnpm start` now re-downloads the bundled curl when the one on disk came from an older datalib release, instead of keeping whatever the dev machine already had. Previously a stale bundle silently survived every version bump.
