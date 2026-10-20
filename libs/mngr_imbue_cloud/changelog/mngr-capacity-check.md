Made cold-box slice bakes robust to transient failures (previously each failed seed build permanently consumed one requested slice, e.g. 3 of 6 requested workspaces lost on one production bake):

- Waiters on the per-box image-cache build lock now notice a dead seeder: `wait_for_tar` exits early when the lock disappears without a published tar, and `_ensure_cached_image_present` re-checks the tar and re-contends the lock every round -- a failed seed hands off to a new builder within seconds instead of stranding waiters for the full 30-minute window.

- Every requested slice is now baked with bounded retries (3 attempts): a failed bake destroys its VM and writes no pool row, so a retry is a clean fresh slice and transient failures (SSH resets, flaky image builds) self-heal instead of shrinking the delivered pool. Terminating the bake (SIGTERM/SIGINT) stops the retry loops too, so a killed bake never spawns replacement slices after its workers are killed.

- `admin pool create` (`allocate_slices`) now runs a seed-first phase on a cold box: one slice is baked alone to build + publish the box image tar before the fan-out, so the remaining slices always take the warm docker-load path, and a build that fails all its attempts aborts the whole bake up front with one clear error.
