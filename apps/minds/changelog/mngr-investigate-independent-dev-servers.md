Multiple developer environments can now safely share a single bare-metal slice box.

`minds pool create --backend slice` now stamps the activated environment into each slice's lima names (forwarded as `--slice-env-name`), so a shared box can attribute every slice to an env and reconciliation only ever touches the right env's slices.

`minds env destroy` now tears down the env's unleased pool slices on their bare-metal boxes before deleting the per-env database, so a destroyed env no longer leaks its baked pool VMs on shared boxes. Leased slices continue to be torn down via their agent's release path.
