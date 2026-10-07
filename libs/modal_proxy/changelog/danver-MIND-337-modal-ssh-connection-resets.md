Changed: `DirectModalInterface.deploy` makes one attempt and reports a concurrent modification of the same app as `ModalProxyAppLockedError`, instead of retrying it internally for up to five minutes (MIND-337).

Modal locks an app for the duration of a mutation, so concurrent deploys of one app name race and all but one are refused. Retrying the refused deploy here could only ever re-take the lock and publish the same thing again, because this layer does not know what was being deployed. Deciding whether a refused deploy is still needed belongs to the caller that does know, so `mngr_modal.routes.deployment.ensure_function_deployed` now owns the retry; this layer classifies the failure and stops.

- `DEPLOY_ATTEMPT_TIMEOUT_SECONDS` (180s, the `modal deploy` subprocess timeout) is now public, so a caller can size its own retry budget from it.

- `DEPLOY_MAX_DURATION_SECONDS` moved to `imbue.mngr_modal.routes.deployment`, which is now what the bound describes.
