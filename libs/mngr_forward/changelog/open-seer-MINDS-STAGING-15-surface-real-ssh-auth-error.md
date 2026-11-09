SSH tunnel setup now reports the authentication failure it actually hit, and closes a connection that fails partway through setup.

`_create_ssh_client` handed the private key to paramiko as a path (`key_filename=`), which makes paramiko try the file as each key class in turn and re-raise only the last failure. When a host rejected an RSA or ECDSA key in PEM format, the error read `SSHException: encountered RSA key, expected OPENSSH key` (or `encountered EC key`) instead of the rejection. The key is now loaded once with `ssh_utils.load_ssh_private_key_file` and passed as `pkey=`, so a rejection is reported as `AuthenticationException: Authentication failed.` for every key type. A key file that is on disk but will not load fails before the host is dialed, as `SSHTunnelError` tagged `LOCAL_SETUP`.

A host with a key of its own is now authenticated with that key alone, so the ssh-agent's keys and `~/.ssh/id_*` can no longer supply the reported failure. Hosts with no key of their own (the `ssh` provider without `key_file`, and outer hosts reached over `DOCKER_HOST=ssh://`), and hosts whose key needs a passphrase, keep using the user's own SSH setup.

A connection whose key was rejected, or whose host key does not match the pin, is now closed. paramiko leaves the transport running when either check fails, and the repair loop retries a failing tunnel indefinitely, so every retry left one more connection open until the host dropped it.

The warning logged for a rejected key now describes the credentials actually offered: the loaded key's fingerprint, or the user's own SSH setup and why.
