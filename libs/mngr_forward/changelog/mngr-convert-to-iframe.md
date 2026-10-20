Embedding substrate for the minds iframe migration: workspace origins can now be embedded cross-site in an iframe by a trusted host application.

- On the TLS path (`--use-http2`) session cookies become `SameSite=None; Secure; Partitioned` so workspace requests authenticate from inside a cross-site iframe. The plain-HTTP path keeps `Lax` (embedding unsupported there).

- New `/_bridge` route + `--browser-bridge-token <secret>`: a host application 302s an already-authenticated browser to `/_bridge?token=...&next=...` to obtain the bare-origin session cookie without consuming an OTP -- the browser twin of the Electron preauth cookie injection.

- The proxy now owns embedding policy: a `Content-Security-Policy: frame-ancestors` header is APPENDED to every proxied workspace response (never touching bodies or existing headers -- multiple CSP headers compose by intersection). The default denies external embedding (`'self'` + the workspace's own origin family); pass `--embedder-origin <scheme://host[:port]>` (repeatable) to allow specific embedders. **Breaking**: previously no header was sent, so any page could iframe a workspace origin.

- TLS leaves are now minted per startup from a persistent local CA under `$MNGR_HOST_DIR/plugin/forward/ca/` (mkcert-style) instead of a regenerated self-signed cert. `mngr forward --trust-ca` installs the CA into the platform trust stores (macOS login keychain / Linux per-user NSS) so plain browsers get no certificate interstitials; the minds Electron app keeps trusting programmatically with no install.
