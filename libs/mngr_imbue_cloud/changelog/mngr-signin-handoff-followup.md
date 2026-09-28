`mngr imbue_cloud auth login`: the browser page at the end of the sign-in now reports the real outcome.

- The page used to say "You're in!" as soon as the browser reached it, before the one-time code was checked or exchanged, so a refused exchange showed success in the browser while the sign-in failed. It now waits for the exchange (up to 30 seconds) and says either that you're signed in, that the sign-in didn't finish, or, if the exchange is still running, to return to the app or terminal.

- A code exchange the connector refuses (expired, already used, or a PKCE mismatch) now fails with `error_class` `ImbueCloudDeviceCodeRefusedError` instead of the generic `ImbueCloudAuthError`, and a callback without a code fails with `LoginCallbackMissingCode` instead of `LoginFailed`, so embedders can tell these apart from an unreachable connector.

- `auth login` takes `--listen-timeout SECONDS` (default 600), how long to keep waiting for the browser to finish. The minds desktop app passes a one-hour window.

- `mngr imbue_cloud auth login` now tells the connector while it is waiting for the browser: it renews a listener lease every 10 seconds and releases it when it stops (including when it is terminated with SIGTERM, as the minds desktop app does on quit). A connector that knows the lease sends a browser that finishes after the listener stopped to an "open Minds and sign in" page instead of the closed local port. Against an older connector the lease calls are ignored and sign-in works as before.
