Creating an Imbue Cloud workspace on the fast path makes fewer round trips between the lease and the started agent. Nothing about what gets installed, rotated or pinned has changed.

- Slice adoption runs all its VM commands over one SSH connection instead of opening a new one for each, and stops probing an endpoint for a host key it generated a moment earlier. After a key install, the first five re-checks of the served key come 0.2 s apart and later ones 1 s apart, within the same 30 s limit. The fast phase stays short so the unauthenticated probes cannot add up to a refusal from an sshd with per-source penalties (OpenSSH 9.8 or later).

- A VM sshd that hangs up on the connection, during the SSH handshake or while a command runs, now fails adoption like any other connection failure (the create continues on the existing trust material, and the next app start retries) instead of aborting the create.

- The fast path no longer waits for the container's sshd before connecting to it, since the lease has just connected to that sshd. It waits and retries only if the first connect fails; a refused host key or client key fails at once.

- While the create holds the host lock, the leased host keeps the host env file and the pre-baked agent's `data.json` in memory until the agent starts, instead of reading them over SSH again and again. In that window the create is the only thing that writes either file: post-host-create commands run before the lock is taken, and if the pre-baked agent state is missing (so full provisioning runs) the host stops keeping them in memory.

- The logs now show how the time splits: how long the lease request took (debug), how long adoption took, and how long the rest of host creation took. Each adoption step also has its own debug span.
