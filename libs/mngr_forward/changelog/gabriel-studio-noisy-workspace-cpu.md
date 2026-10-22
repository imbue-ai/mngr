- `mngr forward` no longer rewrites its on-disk service map when an agent re-announces registrations it already knows. Before, an agent replaying a long services history caused one full rewrite of the cache file per line.

- Each agent's services stream now starts with `mngr event ... --latest-by services:service`, so a (re)spawned stream replays each service's latest registration instead of the agent's whole services history, which on one workspace was 13.9 million lines.
