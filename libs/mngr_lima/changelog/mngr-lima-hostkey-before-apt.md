Fixed a Lima workspace-creation failure that surfaced as a confusing `SSH host key error (Host key for 127.0.0.1 does not match.)`.

The VM provisioning script installed the pre-trusted SSH host key *after* a network-dependent `apt-get install`, all under `set -e`. A transient Debian mirror hiccup (`apt-get` failing to fetch package indexes) would abort the whole script before the host key was installed, so the VM kept its default host key while mngr had already pinned the key it expected -- causing a strict host-key-check mismatch on connect.

The host-key swap (and sshd tuning) now runs first, before any package fetch, so SSH host-key trust no longer depends on the network. The package install is additionally wrapped in a retry loop to ride out transient apt mirror failures; if it still fails, the error now surfaces clearly on an SSH-reachable host instead of as a host-key mismatch.
