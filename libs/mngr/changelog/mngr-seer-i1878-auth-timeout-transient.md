New `imbue.mngr.hosts.outer_host.is_ssh_auth_timeout`: tells whether paramiko gave up waiting for sshd to answer authentication behind an error, which a `HostAuthenticationError` from the outer host otherwise does not distinguish from a rejected key.

Test utilities: `in_process_paramiko_sshd` and `ExecOnlyParamikoServer` take an `is_auth_held` argument, which makes the server answer no public-key auth request until the context exits, standing in for an sshd that never answers authentication before the client gives up.
