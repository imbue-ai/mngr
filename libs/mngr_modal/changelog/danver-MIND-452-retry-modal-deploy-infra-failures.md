Fixed: `mngr create` on Modal no longer fails outright when Modal fails the `snapshot_and_shutdown` endpoint's deploy for a reason of its own -- an image build losing the worker it was running on, or a deploy that hangs past its 180s attempt (MIND-452).

Before this, `ensure_function_deployed` retried only Modal's per-app deploy lock, so during a Modal incident on 2026-10-05 every create that needed the endpoint failed on its first attempt, across three CI runs. It now retries every failure Modal produced without judging the script, under the same elapsed-time budget, re-checking between attempts whether some other deploy has already published this source.

- The retry's constants and `ensure_function_deployed`'s budget parameter drop `lock` from their names (`DEPLOY_RETRY_BUDGET_SECONDS`, `DEPLOY_RETRY_MIN_BACKOFF_SECONDS`, `DEPLOY_RETRY_MAX_BACKOFF_SECONDS`, `retry_budget_seconds`): the lock is now one of several reasons they cover. `DEPLOY_MAX_DURATION_SECONDS` is unchanged.

- The error raised when the budget runs out names the failure that was actually repeating, instead of always reporting a locked app.

- The `test_snapshot_and_shutdown` acceptance tests deploy through `ensure_function_deployed`, the entry point a create uses, so they survive the same Modal-side failures a create does.
