Fixed the tutorial's "destroy all docker agents" one-liner: it now pipes agent
ids into `mngr destroy -f -` (with the `-` stdin placeholder) instead of
`mngr destroy -f`. Without `-`, `mngr destroy` does not read from stdin and
fails with "Must specify at least one agent". This matches every other
filter-into-stdin example in the tutorial (e.g. `mngr list --ids | mngr stop -`).

Also hardened the corresponding release test to create a real Docker agent and
verify it is actually destroyed by the command, rather than only running the
command against an empty agent list.
