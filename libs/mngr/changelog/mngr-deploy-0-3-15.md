Host SSH info now names its known_hosts file explicitly: `SSHInfo` gained an optional `known_hosts_path` field, populated from the connector host data every provider already records, and carried through `mngr list --format json` and the `host_ssh_info` discovery event. Consumers no longer have to guess the pin file's location from the private key's directory.

The human-facing `ssh` command in host SSH info now includes `-o UserKnownHostsFile=<path> -o StrictHostKeyChecking=yes` when the pin file is known, so a copy-pasted command verifies the host key.

The transient SSH connect retry (paramiko "Error reading SSH protocol banner" on freshly booted hosts) now makes three bounded attempts instead of two: a slow fresh Modal sandbox was observed outlasting the single retry, so the worst case for a host that accepts TCP but never speaks SSH grows from ~30s to ~45s in exchange for riding out slower sshd boot windows.

Provider SSH connections now give paramiko a 30-second banner timeout (up from its 15s default, via pyinfra's `ssh_paramiko_connect_kwargs`): degraded Modal tunnels were measured answering the SSH handshake right around 15s, so every connect died with "Error reading SSH protocol banner" no matter how many times it was retried. A genuinely dead endpoint still fails fast at the TCP layer.
