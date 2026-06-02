Harden SSH connections with kernel TCP keepalive and a per-socket retransmit cap.

The kernel's default TCP retransmit budget on an established connection that has gone silent is ~924s on Linux and ~500s on macOS. mngr's SSH connections to remote sandboxes hit this in CI when a sandbox is reaped (or a NAT in the path drops the connection's state) during a long idle window: the next SSH operation -- typically the post-finalize ``stop_agents`` call -- then blocks on retransmits for ~15 minutes before surfacing an error.

This change adds a small ``imbue.mngr.utils.tcp_utils`` module with a ``harden_tcp_socket`` helper that enables ``SO_KEEPALIVE`` and configures a paired keepalive + retransmit-cap budget on the underlying socket of every newly-opened SSH connection (called once from ``OuterHost._ensure_connected`` after a successful ``connect()``). The pairing follows Cloudflare's "When TCP sockets refuse to die" recommendation:

- ``TCP_KEEPIDLE = 60s`` / ``TCP_KEEPINTVL = 30s`` / ``TCP_KEEPCNT = 3`` -- bounds the idle-then-silent case to 150s and refreshes intermediate NAT state.
- ``TCP_USER_TIMEOUT = 150_000ms`` on Linux, ``TCP_RXT_CONNDROPTIME = 150s`` on macOS (the equivalent sockopt, not exposed by Python's socket module so we pass the raw 0x80 value). Covers the active-send-into-silent-peer case where the keepalive timer is suspended because there's unacked data in flight.

Both timers are set to the same 150s total so the connection's time-to-detect-dead-peer is consistent regardless of which timer the kernel is using when failure happens.

End-user effect: ``stop_agents`` (and any other SSH operation that catches a dead sandbox) now surfaces failure in ~150s instead of ~900s, removing the polling-loop wedge that motivated ``libs/mngr_mapreduce/imbue/mngr_mapreduce/agent_stopper.py``. The workaround module can be deleted in a follow-up PR once this has been verified in production.
