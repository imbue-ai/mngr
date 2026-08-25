An SSH connection that the far end has reset is now treated as the transient failure it is, and retried on a rebuilt connection, instead of aborting the command.

This is what a connection cached across a laptop sleep meets when it is next used: the peer dropped it while nothing was running, and the reset arrives on the next command rather than on connecting. It was classified as a permanent error only because its message is the errno text ("[Errno 54] Connection reset by peer") rather than the "Socket is closed" wording the check looked for -- so a command that would have succeeded on a reconnect instead failed, and where nothing caught it, printed a Python traceback. Waking a laptop and having a machine restart itself was one way to see it.

A reset that outlasts the retries still fails, but now reports itself as a connection error like every other kind.
