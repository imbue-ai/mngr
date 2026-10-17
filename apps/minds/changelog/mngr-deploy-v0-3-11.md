`minds server list` gains `--verify-occupancy`, which SSHes every bare-metal box for the activated env and reports its **real** occupancy -- every env's slices, not just this env's `pool_hosts` rows -- along with any cross-tier contamination (a slice stamped for another tier, or an extra key in the lima user's `authorized_keys`). The pool SSH key is resolved from the activated tier's Vault entry, so nothing is exported by hand. The default table is unchanged.

A box that cannot be read -- down, mid-reinstall, no recorded address -- is listed as unaudited instead of ending the run, so one bad box never costs you the rest of the fleet's verdict, and unaudited is reported separately from clean.

This closes a blind spot that had real consequences: the DB-derived slot columns count only the querying env's rows, so a box shared with another env reads as emptier than it is. Acting on that number meant discovering the truth only when a bake refused with "server ... has only N of M slot(s) free". Baking onto a cross-tier box now fails closed (see the `mngr_imbue_cloud` entry); `--verify-occupancy` is how you find such a box *without* a failed bake.

`tier_for_env_name` now delegates to `mngr_imbue_cloud.primitives` rather than keeping its own copy of the env-name -> tier mapping, so the minds CLI and the new bake-time guard cannot disagree about which tier an env belongs to. Behavior is unchanged.

Fixed a workspace on a cloud provider being shown as **unreachable** at startup when it was healthy and reachable the whole time.

On startup the desktop client replays the discovery events-file backlog and deliberately drops the errors those pre-start snapshots carry, since they describe the gap while minds was closed rather than the present. It was still recording each dropped snapshot's timestamp as the provider's freshness, and the landing page reads "a snapshot newer than the host's key, with no provider error" as proof that discovery looked for the host and did not find it. A provider that had been failing for the entire gap therefore read as healthy-reporting-zero-hosts, and every workspace on it rendered the positive claim "unreachable" until the first genuine post-start snapshot arrived.

Dropping the error now also withholds that snapshot's freshness, so such a workspace correctly shows "connecting" -- we do not know yet -- until real discovery data lands. A pre-start snapshot that carried no error is a real observation and still records freshness, unchanged.
