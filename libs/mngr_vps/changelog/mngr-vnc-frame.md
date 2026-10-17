The VPS providers publish the ports declared with `mngr create --publish <port>[/<protocol>]` (`specs/published-ports/spec.md`): the container realizer adds `-p 0.0.0.0:<port>:<port>/<protocol>` beside the sshd mapping, and the endpoints (the VM's public address at the guest port number) are recorded on the host for `mngr list --format json`.
Declaring port 22 is refused, since it is the container's sshd.

Cloud providers with a managed firewall gain the config key `firewall_published_ports` (a list of `<port>[/<protocol>]`, validated at load): their `prepare` opens those ports from every source, and a create declaring a port outside the list fails before any resource is created.
`run_container` now takes full `-p` specs (`publish_specs`) instead of a host-bind to container-port mapping, so a guest port declared for both protocols can be expressed.
