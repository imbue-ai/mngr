Follow-ups to the event-driven follow-tail work (PR #733), addressing review feedback:

- `DirectoryWatchGroup` now supports multiple subscribers per watched directory: `watch` adds a (directory, wake event) subscription instead of silently replacing any previous wake event for that directory, and `unwatch` removes only the caller's own subscription, releasing the underlying filesystem watch when the last one goes. This removes the footgun where one caller's unwatch could break another caller's watch on the same directory.

- The per-source follow tails in `mngr event --follow` now bridge the stream's stop event into their directory-watch wake event via a parked forwarder thread (matching the discovery-log tail), so a tail sleeping on a directory watch reacts to shutdown immediately and independently of `wake_all()` registration timing.
