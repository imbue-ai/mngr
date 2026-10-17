`mngr imbue_cloud shares create` gains `--preferred-region`, which steers a first-time share of a local workspace to a specific relay region (ignored for pool hosts, unknown regions, and re-shares -- the existing region sticks).

New `mngr imbue_cloud shares relays` subcommand (and `list_share_relays` on the connector client) shows the relay fleet as a region -> tunnel-control endpoint map plus the default region, so clients can pick a preferred region by measuring their own latency. A malformed relays response raises an error rather than degrading to an empty map.
