`mngr event --follow` now exits with `Error: Stopped following events: the reader for source '<source>' died (...)` when one of its readers dies. Before, it kept running and looked healthy while delivering nothing from that source. Full `mngr observe` (as run by `mngr notify`) already fails when one of its per-host follows fails, so it now exits with an error there instead of silently losing that host.

paramiko's `SFTPError` ("Garbage packet received", seen when an SFTP read is interrupted mid-packet, as when the suspension watchdog closes a connection after a laptop sleep) is now retried on a fresh connection like other broken-connection errors. It used to slip past every SSH error handler, and it was what killed those readers.

Repeated failures to check whether the followed host is online are now logged: debug on the first, one warning after ten.

New developer doc `docs/sleep_and_connectivity.md` (linked from `docs/architecture.md`) collects how clocks, SSH connections, and timeouts behave across laptop sleeps, DarkWakes, and lost networks, and what new timeouts and liveness checks should do about it.
