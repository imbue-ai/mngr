Removed a way `mngr create` on Modal could fail outright: `Error: SSH server not ready after 60.0s at <host>:<port>` (MIND-234).

Every `mngr create` takes an "initial" snapshot of the new host at the end. Recording that snapshot used to resolve a host of its own and open a second SSH connection to write the snapshot record -- a fresh handshake, right after a filesystem snapshot that can leave the sandbox's tunnel unable to complete one. The code waited for the tunnel with a 60-second sshd readiness probe, and when the tunnel did not answer within that, the create failed and the new host was torn down.

Recording a snapshot now uses the host the caller already holds, connected before the snapshot runs. A connection opened beforehand keeps working straight through a snapshot, so there is nothing to wait for: the readiness probe is gone, and so is the second SSH connection every create used to open (about six seconds per create).

`mngr snapshot create` and the pre-termination snapshot in `mngr stop` take the same path and get the same fix.
