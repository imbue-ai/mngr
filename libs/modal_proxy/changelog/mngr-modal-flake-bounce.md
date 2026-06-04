Retry `modal deploy` when Modal reports "The selected app is locked - probably due to a concurrent modification".

Modal serializes mutations to a single app, so two operations targeting the same app name concurrently (e.g. parallel `mngr create` against the same persistent provider app, which redeploys the snapshot/shutdown function on every create) would race and one would fail with an app-lock error. The lock is transient -- it clears as soon as the conflicting operation finishes -- so `DirectModalInterface.deploy` now classifies it as a new retryable `ModalProxyAppLockedError` and rides through it with exponential backoff. Non-lock deploy failures are still raised immediately without retry.

This also removes a frequent flake in the Modal acceptance tests, where many subprocess `mngr create` tests share one persistent app within a shared-environment offload run and deploy into it concurrently.
