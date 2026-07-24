Kanpan refreshes now stream in: instead of waiting for the slowest data source (the GitHub PR/CI fetch) before the board updates, kanpan lists the agents, paints the board immediately, and then fills in each column the moment its data source returns.

To keep the board from visibly reshuffling on every refresh, the initial paint is seeded from the last known values (rendered greyed-out as stale), so each agent stays in its previous section until fresh data lands and updates it in place. The first refresh of a session, which has no cached values yet, simply shows the agent list and fills in as sources return.

The final board is unchanged: once every source has reported, a fresh value fully governs its column (a stale seeded value it does not reproduce is dropped), so the result matches the old all-at-once fetch. A source that errors keeps its last-known value on the board rather than blanking the column.
