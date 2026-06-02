Added a robust "slow path" to imbue_cloud host leasing. A new `fast_mode` build
arg (`-b fast_mode=require|prevent`) selects how `mngr create` lands on a pool
host:

- `fast_mode=require`: lease a pool host whose attributes exactly match and adopt
  its pre-baked agent (the original fast path). Raises a distinct
  `FastPathUnavailableError` when no exact match exists.
- `fast_mode=prevent` (the new default): lease any adequately-sized available
  host (resource attributes only; `repo_branch_or_tag`/`repo_url` are dropped),
  destroy its baked container, and rebuild it from the FCT Dockerfile via the
  shared `mngr_vps_docker` setup path, then run mngr's standard full client-side
  setup -- exactly like an OVH host.

Once a host is leased, any failure during the remaining setup now releases the
lease back to the pool before re-raising, so failed builds never leak a lease.
Logs clearly state which path was taken (`FAST PATH` vs `SLOW PATH`).

Unknown `-b` entries (e.g. `--file=Dockerfile`, `.`) are now forwarded verbatim
to the delegated build instead of being rejected.
