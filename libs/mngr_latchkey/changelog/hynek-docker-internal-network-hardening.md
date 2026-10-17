# VPS provisioning fences the bridge-bound services off every interface but the docker bridge

The VPS-resident latchkey gateway and the owner-exec daemon bind the VPS's
docker bridge address, but binding an address does not bind an interface:
Linux delivers a packet for any local address whichever interface it arrives
on, so a packet for the bridge address that reached the VPS's public interface
(from a neighbour on the same segment, or a route for the private range
pointing at the VPS) landed on either service. Remote provisioning now installs
`nftables` on the VPS and, before starting either service, loads a policy (its
own table, `inet mngr_bridge_services`, at `/etc/nftables.d/mngr-bridge-services.nft`)
that drops traffic to their ports unless it arrives on `docker0` or on
loopback. Loopback stays open because the VPS's own processes reach the gateway
there (the compatibility reverse tunnel among them). The same policy confines
the other direction too: the bridge address is the container's default
gateway, through which it could reach every service the VPS binds on all
interfaces (its sshd, for one), so a new connection arriving on `docker0` for
any port but those two is dropped as well. Established traffic passes, so a
connection the VPS opens into the container keeps its replies, and the
container's internet traffic is forwarded rather than delivered to the VPS, so
it is unaffected. A systemd oneshot
(`mngr-bridge-services-firewall.service`) re-loads the policy at boot, ordered
ahead of docker and the owner-exec daemon -- which systemd brings back after a
reboot -- so the load runs before the daemon listens. A VPS the policy cannot be
applied on fails provisioning (retried on the next discovery cycle) rather
than starting the services unfenced.

The bridge interface name now lives in `docker_bridge.py` as
`DOCKER_BRIDGE_INTERFACE_NAME`, shared by the address resolution and the
policy. The latchkey e2e release test asserts the table is loaded on the VPS.
