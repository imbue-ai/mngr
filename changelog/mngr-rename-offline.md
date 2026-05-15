`mngr rename` now works against offline hosts: when the agent's host is
not online, the rename (and any `-l KEY=VALUE` labels) are written to the
provider's persisted agent data without starting the host. The
`--start/--no-start` flag has been removed from `rename` because the
command no longer needs to start a host.
