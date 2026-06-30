The imbue_cloud provider now reports the container's outer-host-loopback SSH port (`get_container_loopback_ssh_port`), which is the fixed in-VM publish port (`config.container_ssh_port`) rather than the externally-routable, box-forwarded port a remote client connects to.

This lets the VPS-resident latchkey gateway reverse-tunnel into the agent's container on the correct port. On a lima slice the publish and connect ports differ, and using the connect port broke the tunnel, leaving agents without latchkey access whenever the desktop gateway was offline.
