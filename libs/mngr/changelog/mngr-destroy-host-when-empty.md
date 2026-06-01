`mngr destroy` now actually destroys the host when the last agent on it
is destroyed -- the documented contract -- regardless of how recently the
host was created. Previously this fired through the post-destroy GC pass,
which gates on `min_online_host_age_seconds` (default 10 minutes), so any
host destroyed within minutes of creation leaked its cloud-side resources
(e.g. an active imbue_cloud lease, a Vultr VPS) until the 7-day
destroyed-host grace period eventually triggered `provider.delete_host`.

Two changes in `destroy.py`:

1. **Partition step reconciles discover-vs-on-host disagreement.** When
   every matched agent is a "ghost" -- returned by the provider's discover
   but absent from the host's own `get_agents()` -- the destroy CLI now
   escalates to host-level destruction (`provider.destroy_host`) instead
   of silently dropping the match. This is what was producing the
   "No agents found" message when the same agent was destroyed twice on
   an imbue_cloud-leased host: the first destroy removed `/mngr/agents/<id>/`
   on the VPS but the connector's lease list still reported the agent.

2. **Post-loop sweep destroys hosts whose last agent was just destroyed.**
   For each online host that had at least one agent destroyed in this
   invocation, the destroy CLI now re-checks `host.get_agents()` and, if
   empty, calls `provider.destroy_host` directly. Bypasses the GC's
   `min_online_host_age_seconds` filter; the GC pass that runs immediately
   after is the safety net for transient failures.

Net effect: cloud-side resources are released the moment `mngr destroy`
returns, and the destroyed-host grace period only retains historical
state -- aligning all provider types with the same semantic that the
docker / mngr_vps_docker / imbue_cloud `destroy_host` implementations
already implement individually.

## New: `--post-host-create-command`

`mngr create` learns a new repeatable flag, `--post-host-create-command`,
that runs one or more shell commands inside a newly-created host
synchronously after the host is online but before any agent work_dir is
touched. Each command runs in order via the host's normal exec path; a
non-zero exit aborts the create. Stackable from `create_templates.<name>`
via `post_host_create_command__extend = [...]`.

Motivation: an image may need first-boot setup (e.g.
forever-claude-template seeds a baked workspace from `/docker_build_code`
onto its `/mngr/` volume) that must complete before mngr's git mirror
push or any other work_dir setup. Until now this had to be encoded in the
container's `CMD`, which raced against mngr's `docker exec` calls and
required an FCT-specific `use_image_default_cmd` opt-out in
`mngr_vps_docker` / `providers.docker`. The opt-out and the
defensive `--workdir /` exec override (from commits `d77714cdf` /
`55c420c35`) are reverted in the same commit -- the new generic hook
replaces both.
