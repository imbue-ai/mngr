Listing agents (`mngr list`, and the board fetch built on it) now bounds each host's
live detail collection by a wall-clock budget instead of waiting on every host without limit.
While the budget is active, each of the host's reads self-terminates within the remaining time,
so a slow or contended host (slow tmux/ssh, a wedged provider) falls back to its offline/partial
data rather than stalling the whole read -- and nothing is left running in the background. The
budget is configurable via `host_detail_read_timeout_seconds` (default 20s).

The per-host detail build also stopped issuing a redundant boot-time probe: it previously ran
`sysctl kern.boottime` (or the Linux equivalent) twice per host per fetch -- once for boot time
and once for uptime. A single host probe now returns both, with uptime measured on the host so
it is unaffected by clock skew between the client and the host.
