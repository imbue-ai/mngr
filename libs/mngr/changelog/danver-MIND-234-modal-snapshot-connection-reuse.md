Added `HostInterface.connect()`, the inverse of the existing `disconnect()`: it establishes a host's connection immediately instead of on the next operation. It is a no-op by default, and opens the SSH connection for online hosts.

Callers need this when a later operation has to ride a connection that already exists -- for instance across a window in which the host's endpoint will not complete new handshakes. The Modal provider uses it to record a filesystem snapshot over a connection opened before the snapshot (MIND-234).
