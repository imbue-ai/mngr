Read-only Modal control-plane calls now wait out a transient Modal failure instead of failing the caller's whole command (MIND-312).

Retry used to be attached to `DirectVolume`'s methods and nowhere else, so whether a blip was survived depended on which object the call hung off rather than on the failure. `sandbox_list`, `Sandbox.get_tags()`, `Sandbox.poll()`, `Sandbox.tunnels()`, `sandbox_from_id`, `volume_list`, `is_function_deployed` and the app lookup -- the reads every discovery makes -- had none at all, so one transient status from Modal ended the operation immediately. They all share one retry policy now, keyed on the error.

The budget is stated in seconds rather than in attempts, and is 60s. Modal's own client gives a failing control-plane connection a 63s `total_timeout`, which is its statement of how long its blips last; the previous `stop_after_attempt(5)` amounted to about 15s, and CI showed Modal `INTERNAL`s outlasting it.

Backoff now carries up to 2s of jitter. Discovery fans dozens of volume reads out at once, so a blip fails them all at the same instant; an undithered curve marched them back in lockstep, which is how waiting out a blip provokes Modal's rate limit. The jitter spreads each wave out rather than reducing the fan-out.

Each retry is now logged at warning level with the error and how much of the budget is spent, so how long Modal's blips actually last is visible rather than guessed at.

Mutations (creating a sandbox, setting tags, taking a filesystem snapshot, deleting a volume) are deliberately not retried: Modal may have applied one before failing to report on it.
