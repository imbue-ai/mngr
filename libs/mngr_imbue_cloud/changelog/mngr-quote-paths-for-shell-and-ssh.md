The pinned `known_hosts` path in the lima slice client's ssh commands is now quoted for ssh.

ssh documents `UserKnownHostsFile` as a whitespace-separated list of files and splits the value itself, so a path containing a space became several nonexistent files and every command against the slice box failed host-key verification. The file is written beside the pool private key, so a key directory whose name contains a space produces one without anyone choosing it.
