Merge the web-only work (`mngr/hopefully-last-web-details`) into main.

SSH client keypair generation (`generate_ssh_keypair`, used by the docker, modal, lima, vps, and imbue_cloud providers) now produces Ed25519 keys in OpenSSH format instead of RSA-4096 PEM. Ed25519 is required by the minds workspaces' owner-exec envelope auth, so the key a client SSHes with can also sign exec requests. Every consumer auto-detects the key type and existing RSA keypairs on disk keep working; generation only happens when no pair exists yet.

Regenerated the `mngr imbue_cloud` CLI reference docs for the reworked auth commands (browser-based `auth login` replacing `auth oauth`, `auth signout --all-devices`, `auth is-verified` as a plain status query) and the new `shares create --entry-label` option and `hosts enable-sharing` command.
