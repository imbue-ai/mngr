Fixed: a `modal deploy` that Modal failed of its own accord is now reported as something a caller can retry, instead of as a terminal error (MIND-452).

- `DirectModalInterface.deploy` classifies "Worker disappeared, image build failed" -- Modal's image builder losing the worker a build was running on -- as `ModalProxyImageBuilderLostError`. A build that failed on its own contents, such as a command in the image exiting non-zero, is unaffected and still terminal.

- A deploy that outruns the 180s one attempt is given now raises `ModalProxyDeployTimeoutError` instead of letting `subprocess.TimeoutExpired` escape. That was also a hole in this package's promise that callers only ever see `ModalProxy*` errors, since no caller can catch a type it is never told about.

- New `ModalProxyDeployRetryableError` is the single statement of which deploy failures warrant another attempt. `ModalProxyAppLockedError` now subclasses it, so `except ModalProxyAppLockedError` keeps behaving as before.

- `DirectModalInterface` takes a `deploy_attempt_timeout_seconds`, defaulting to and bounded above by `DEPLOY_ATTEMPT_TIMEOUT_SECONDS`. Only shortening it is allowed, because `imbue.mngr_modal.routes.deployment` sizes `DEPLOY_MAX_DURATION_SECONDS` from that constant.
