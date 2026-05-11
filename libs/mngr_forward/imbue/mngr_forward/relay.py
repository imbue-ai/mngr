"""Bidirectional relay between a local socket and a paramiko channel.

Used by both the SSH tunnel (forward direct-tcpip) and reverse port-forward
paths. Lives here so the relay-spin fix for half-closed channels stays in a
single place.
"""

import select
import socket
from typing import Final

import paramiko
from loguru import logger

_BUFFER_SIZE: Final[int] = 65536

_SELECT_TIMEOUT_SECONDS: Final[float] = 1.0


def relay_step(sock: socket.socket, channel: paramiko.Channel) -> bool:
    """Perform one relay step: transfer available data between sock and channel.

    Returns True to continue relaying, False when either end has closed.
    """
    r, _, _ = select.select([sock, channel], [], [], _SELECT_TIMEOUT_SECONDS)

    if sock in r:
        data = sock.recv(_BUFFER_SIZE)
        if not data:
            return False
        channel.sendall(data)

    if channel in r:
        if channel.recv_ready():
            data = channel.recv(_BUFFER_SIZE)
            if not data:
                return False
            sock.sendall(data)
        elif channel.eof_received or channel.closed:
            # Paramiko marks the channel's fileno readable on EOF/close as well
            # as on data arrival, but recv_ready() only goes True for data. Without
            # this branch the relay loop would spin at ~1M iters/sec on a half-closed
            # channel until something else tore it down.
            return False
        else:
            # select woke us for a non-data channel event (e.g. window-adjust);
            # keep looping so the next iteration re-checks for data or EOF.
            pass

    return True


def relay_data(sock: socket.socket, channel: paramiko.Channel) -> None:
    """Relay data bidirectionally between a local socket and a paramiko channel.

    Uses select() to multiplex reads from both ends. Terminates when either
    end closes or an error occurs.
    """
    try:
        while relay_step(sock, channel):
            pass
    except (OSError, EOFError, paramiko.SSHException) as e:
        logger.trace("SSH tunnel relay ended: {}", e)
    finally:
        try:
            channel.close()
        except (OSError, paramiko.SSHException) as e:
            logger.trace("Error closing SSH channel in relay: {}", e)
        try:
            sock.close()
        except OSError as e:
            logger.trace("Error closing socket in relay: {}", e)
