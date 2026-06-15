## Provider uniformity review (user-visible behavior)

- Adds `specs/provider-uniformity-review.md`: a cross-provider review of user-visible behavior across all nine `mngr` provider plugins (Modal, AWS, Azure, GCP, Vultr, OVH, Lima, Docker, SSH). Orchestrated as 8 parallel category subagents (Create UX / List / Stop-Start / Destroy / Snapshots / Credentials / Operator-setup-and-idle / Test contracts) plus one covering the smaller providers, then synthesized into a single report with a Top-20 findings table and 21 cross-cutting recommendations.

- Highest-priority findings: Azure/GCP `mngr stop --stop-host` is a silent cost leak (container only, VM keeps billing); Azure `auto_shutdown_minutes` only OS-halts (still bills); AWS defaults `allowed_ssh_cidrs = ("0.0.0.0/0",)` while Azure/GCP fail-closed; no auto-snapshot on AWS/Azure/GCP so hard-crash recovery is silently absent; idle-driven self-stop only on Modal+AWS; Vultr/OVH lack pytest orphan scanners; AWS/GCP `ProviderUnavailableError` falls through to default "start Docker" help text.

- Source location is `specs/` rather than `libs/mngr_*/`. The user noted this placement is provisional and can be moved when a more appropriate destination is chosen.
