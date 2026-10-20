Marked operations executed with `x` now run several at a time instead of strictly one after another. Marked agents are independent, so a batch no longer costs the sum of its parts -- which mattered most for commands that block on the agent, such as `mngr message`, where messaging five agents could take five times as long as messaging one.

Four run at once by default, tunable (set it to 1 for the previous strictly-sequential behaviour):

```toml
[plugins.kanpan]
batch_concurrency = 8
```

The in-progress footer now counts finished work (`Executing 2/5`) rather than naming a single operation, since several are in flight at once.

Batch work also moved off the shared worker that serves board refreshes, so a long batch no longer holds up the next refresh. Failure handling is unchanged: a failed operation keeps its mark so it can be retried, and the failures are listed at the bottom of the board.

Quitting during a batch drops whatever has not started yet, so `q` returns you to the shell instead of waiting on operations you walked away from; anything already running is left to finish.

Executing a delete mark no longer jumps the board. The view is held in place across a refresh by anchoring on the focused row, and deleting removes exactly that row -- so it now falls back to the nearest surviving neighbour instead of dropping the anchor and rendering from the top. An open search still re-anchors itself to the row it started from.
