Baking pool-host slices now refuses to run against a bare-metal box that is not exclusive to the activated env's tier. Tiers are isolated by construction -- each has its own pool-management SSH keypair, and there is meant to be zero cross-tier reach -- so a box serving two tiers is a box each tier's operators and connector can SSH, and via `limactl` control, the other's workspaces on; neither tier's reap will ever reclaim the other's slices either. Nothing enforced that at the moment it mattered, and a staging box silently ended up carrying a dev env's slice -- which the DB-derived `admin server list` slot table, counting only the querying env's own rows, never showed.

`mngr imbue_cloud admin pool create` now preflights the chosen box before carving anything, and refuses on either of the two ways a box drifts across tiers:

- a **foreign-tier slice** already on the box (its lima name is stamped with an env belonging to another tier). Sharing a box *within* a tier is unaffected -- several `dev-<user>` envs on one dev box stays legitimate and is explicitly allowed.

- an **extra key** in the lima service user's `authorized_keys`. `admin server prep` writes that file with a single-key overwrite, so a second key can only have been added out of band, and it grants another tier SSH access to the box (and via `limactl`, to every workspace on it). No comparison against our own public key is needed: the bake has already authenticated with this tier's pool key, so if exactly one key is authorized, it is necessarily ours.

`mngr imbue_cloud admin server list` gains `--verify-occupancy` (with `--env-name`), which SSHes each box and reports its real occupancy across all envs plus any cross-tier contamination, and warns when a box would fail the bake preflight. The default table is unchanged and still DB-derived.

A box the audit cannot read -- down, mid-reinstall, no recorded address, no pinned host key -- is reported as `unaudited` rather than aborting the run, so one bad box never costs you every other box's verdict (which is the whole point of a fleet audit). Unaudited boxes are counted and warned about separately: unknown is not the same as clean.

`tier_for_env_name` is now defined here (`mngr_imbue_cloud.primitives`) as the single source of truth; `imbue.minds` re-exports it instead of keeping a second copy, so the guard and the minds CLI cannot drift on which tier an env belongs to.

`mngr imbue_cloud admin server await-delivery` no longer aborts on the propagation gap after OVH assigns a serviceName.

OVH assigns the serviceName on the *order* before that service becomes queryable under `/dedicated/server`, so a read landing in the gap gets `404 This service does not exist`. That was raised straight out of the delivery poll, failing the whole step for a race that clears itself within about a minute -- hit on a real production order, where one of two boxes ordered seconds apart failed and its twin sailed through. The 404 is now treated as the same "not delivered yet" signal as a published service with no IP, so the existing poll loop simply keeps waiting. It is tolerated for 15 minutes and then raised with a message naming the serviceName, so a name that never materializes still surfaces promptly instead of polling silently until the 4h delivery timeout. Non-404 API errors (auth, quota, server) are unchanged and still propagate immediately.
