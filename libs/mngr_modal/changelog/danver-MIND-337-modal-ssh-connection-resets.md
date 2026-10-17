Fixed: A `mngr create ... @.modal` that stalls now says what it is waiting for, and concurrent creates against a cold Modal app no longer queue behind each other's identical deploys (MIND-337).

Creating a Modal host waits for the app's `snapshot_and_shutdown` endpoint to be deployed, for up to 495 seconds. That endpoint belongs to the Modal app rather than to the host being created, so on an app that carries no deploy yet every concurrent create finds the source marker missing, every one of them deploys, and they queue behind Modal's per-app deploy lock.

That wait used to be logged below the default level, so the last thing a stalled create said was `Waiting for sshd to be ready...` -- the step before it, which had already finished. Every report of a Modal create hanging therefore read as a bring-up readiness problem, including this ticket and MIND-314 before it. Measured against real Modal, `_wait_for_sshd` takes about 3 seconds of its 60-second budget.

- `ensure_function_deployed` now re-checks the source marker on every attempt, not just the first. A create whose deploy is refused the app lock adopts the endpoint that the winning deploy published, rather than taking the lock again to publish the same source and making every create still queued behind it wait for that too. It also owns the lock retry, which `imbue.modal_proxy.direct.deploy` used to perform internally.

- A create that does not get the endpoint URL within 60 seconds now says `Waiting for app <app>'s Modal snapshot endpoint to finish deploying ...`, and reports how long it waited once the wait completes.

- Spending the whole budget now raises an error naming the app and the budget, instead of `Future.result`'s empty `TimeoutError`.

- Nothing is logged when the endpoint is already deployed, which is every create after the first against a given app.
