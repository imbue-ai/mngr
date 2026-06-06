`mngr destroy` now supports `--on-error abort|continue` (default `abort`).
Previously, running a batch destroy with `--force` (e.g.
`mngr list --ids | mngr destroy - --force`) would discard the entire batch and
destroy nothing if even one id was stale. Now `--force` is confirmation-skip
only: with `--on-error continue`, destroy removes the agents that exist and
warns about the missing identifiers; with the default `--on-error abort`, it
aborts (raising) when any named agent is not found.
