`mngr azure prepare` adds an inbound allow rule from every source to the NSG for each port listed in the provider config's `firewall_published_ports` (or passed as `--publish PORT[/PROTOCOL]`), beside the SSH rules.
A `mngr create --publish` on this provider must declare only ports in that list; any other declaration fails before any resource is created (`specs/published-ports/spec.md`).
