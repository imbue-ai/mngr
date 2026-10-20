A command blocked on an SSH connection that a laptop sleep killed is now released when the laptop wakes, instead of waiting for the kernel to give up.

Nothing above the socket could cut it short: pyinfra's per-command timeout never fires in mngr's usage (a blocked paramiko read starves the gevent hub it runs on), SSH keepalives never wait for a reply, and through a NAT mapping the sleep killed the peer's reset never arrives, leaving only TCP retransmission timing out a minute or two later. One `mngr start` measured just under two minutes of that on a host that was up.

Every SSH connection mngr runs commands over is now stamped with both clocks and watched; when they diverge, connections built before the gap are closed, which wakes everything blocked on them, and the existing retry reconnects.
