"""Low-level TCP socket hardening against silent peers and NAT idle drops.

This module exists for one specific failure mode: a long-idle TCP connection
where either the peer has gone silent (no FIN/RST) or a NAT / stateful firewall
in the path has reaped its translation state. In both cases the next send sits
waiting for the kernel's TCP retransmit budget to expire -- ~924s on Linux
defaults, ~500s on macOS. mngr's SSH connections to remote sandboxes have hit
this in CI; see ``libs/mngr_mapreduce/imbue/mngr_mapreduce/agent_stopper.py``
for the production incident that motivated this.

The fix has two complementary parts that together cover both socket states
(idle vs unacked-data-pending), per Cloudflare's "When TCP sockets refuse to
die" recommendation (https://blog.cloudflare.com/when-tcp-sockets-refuse-to-die/):

1. **Kernel TCP keepalive** (``SO_KEEPALIVE`` + ``TCP_KEEPIDLE`` / ``TCP_KEEPINTVL``
   / ``TCP_KEEPCNT``). The kernel sends bare TCP probes during idle, tracks the
   probe ACKs, and tears the socket down after N missed responses. Side effect:
   refreshes intermediate NAT entries so they aren't reaped during legitimate
   idle periods.

2. **Per-socket retransmit cap** (``TCP_USER_TIMEOUT`` on Linux,
   ``TCP_RXT_CONNDROPTIME`` on macOS). Bounds the time the kernel will spend
   retransmitting unacked data on an established connection. Covers the case
   where the peer dies *while* we're actively sending -- the keepalive timer
   is suspended whenever there's data in flight, so without this knob the
   retransmit budget defaults back to the kernel ceiling (``tcp_retries2`` /
   ``TCP_MAXRXTSHIFT``).

The two are paired so the connection's max time-to-detect-dead-peer is the
same regardless of which timer is in charge: the per-socket retransmit cap is
set to the same total as the keepalive budget
(``TCP_KEEPIDLE + TCP_KEEPINTVL * TCP_KEEPCNT``).
"""

import socket
import sys

from loguru import logger

# Idle seconds before the kernel starts sending TCP keepalive probes. Chosen
# well below any plausible NAT idle timeout (AWS NAT Gateway: 350s; typical
# cloud LB/NAT: minutes) so probes refresh NAT state before it's reaped.
_KEEPIDLE_SECONDS = 60

# Seconds between keepalive probes.
_KEEPINTVL_SECONDS = 30

# Missed probes before the kernel marks the socket dead.
_KEEPCNT = 3

# Total keepalive budget = 60 + 30 * 3 = 150s. This is also the per-socket
# retransmit cap (TCP_USER_TIMEOUT / TCP_RXT_CONNDROPTIME), so the
# in-flight-data and idle paths give the same total time-to-failure.
_USER_TIMEOUT_SECONDS = _KEEPIDLE_SECONDS + _KEEPINTVL_SECONDS * _KEEPCNT

# macOS-specific: TCP_RXT_CONNDROPTIME isn't exposed by Python's socket
# module. Raw value from xnu/bsd/netinet/tcp.h ("time after which tcp
# retransmissions will be stopped and the connection will be dropped").
_DARWIN_TCP_RXT_CONNDROPTIME = 0x80


def harden_tcp_socket(sock: socket.socket) -> None:
    """Enable TCP keepalive and bound the retransmit budget on ``sock``.

    Should be called on a freshly-connected TCP socket. Safe to call repeatedly;
    sockopt setting is idempotent. Per-failure errors are logged at debug and
    swallowed -- this is defensive hardening, not load-bearing for the
    connection to function, and a setsockopt failure (e.g. on an unusual
    platform) should not break the surrounding operation.

    Linux and macOS are supported; other platforms degrade to no-op where the
    relevant sockopt names aren't available.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if sys.platform == "linux":
            _apply_linux_keepalive_and_user_timeout(sock)
        elif sys.platform == "darwin":
            _apply_darwin_keepalive_and_user_timeout(sock)
        else:
            # Unknown platform: leave SO_KEEPALIVE enabled (set above) and
            # accept the platform defaults for the keepalive intervals and
            # retransmit cap; mngr targets Linux + macOS only.
            pass
    except OSError as exc:
        logger.debug("Could not harden TCP socket: {}", exc)


def _apply_linux_keepalive_and_user_timeout(sock: socket.socket) -> None:
    if hasattr(socket, "TCP_KEEPIDLE"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, _KEEPIDLE_SECONDS)
    if hasattr(socket, "TCP_KEEPINTVL"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, _KEEPINTVL_SECONDS)
    if hasattr(socket, "TCP_KEEPCNT"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, _KEEPCNT)
    if hasattr(socket, "TCP_USER_TIMEOUT"):
        # Linux TCP_USER_TIMEOUT is in milliseconds.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, _USER_TIMEOUT_SECONDS * 1000)


def _apply_darwin_keepalive_and_user_timeout(sock: socket.socket) -> None:
    # On Darwin, the sockopt that means "idle time before probes" is spelled
    # TCP_KEEPALIVE (vs Linux's TCP_KEEPIDLE); same semantics, different name.
    if hasattr(socket, "TCP_KEEPALIVE"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, _KEEPIDLE_SECONDS)
    if hasattr(socket, "TCP_KEEPINTVL"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, _KEEPINTVL_SECONDS)
    if hasattr(socket, "TCP_KEEPCNT"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, _KEEPCNT)
    # TCP_RXT_CONNDROPTIME is in seconds on Darwin, and unlike Linux's
    # TCP_USER_TIMEOUT it isn't exposed by Python's socket module -- pass the
    # raw sockopt number.
    sock.setsockopt(socket.IPPROTO_TCP, _DARWIN_TCP_RXT_CONNDROPTIME, _USER_TIMEOUT_SECONDS)
