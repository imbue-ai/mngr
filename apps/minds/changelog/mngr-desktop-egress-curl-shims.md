The desktop app now bundles its curl shims from the `imbue-ai/latchkey-curl-shims` release (pinned at v0.4.0) instead of the datalib release. The desktop latchkey gateway's `LATCHKEY_CURL` is the bundled `latchkey-curl-router`, which fronts `curl-impersonate`. Chrome TLS impersonation for marked requests behaves as before.

The shims are now bundled on every macOS and Linux platform the app builds for, which adds Intel Macs. Only Windows still has none, because the release has no Windows build; latchkey uses the system curl there, without Chrome TLS impersonation.
