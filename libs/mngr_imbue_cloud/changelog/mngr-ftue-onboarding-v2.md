Creating an Imbue Cloud workspace on the fast path makes fewer remote calls between the lease and `created`. What the create writes and what it reports are unchanged.

- The host the fast path builds answers its published endpoints from the lease response it was built from, so the discovery events emitted at the end of the create and the `created` line no longer list the account's leased hosts on the connector to find them.

- The fast-path host also keeps the host env file and the pre-baked agent's `data.json` in memory before the host lock is taken, from when it is built until the first command or outer-host step (a post-host-create command may rewrite either file), so the latchkey hook no longer reads back the env file the create has just written. Taking the lock still starts from what is on disk.

- While the create holds the lock, the duplicate-name check's read of every agent's `data.json` now also fills that memory, so the work-dir step and the start's lookup of the pre-baked agent no longer read its `data.json` again.
