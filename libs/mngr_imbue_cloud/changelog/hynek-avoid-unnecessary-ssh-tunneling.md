# Comment follow-up for the docker-bridge latchkey gateway route

`get_container_loopback_ssh_port`'s comment no longer says the VPS-resident
latchkey gateway must reverse-tunnel into every container: with `mngr_latchkey`
reaching that gateway over the container's docker bridge, the loopback publish
port only matters for a container that predates that route. No behavior change.
