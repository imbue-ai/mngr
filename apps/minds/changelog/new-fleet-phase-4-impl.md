Phase 4 of the slice-fleet cutover: operator docs.

- New runbook `docs/deploy/gen2-cutover.md` for the per-tier gen-1 -> gen-2 cutover window (`minds-admin cutover`): prerequisites, announcement text, the pre-window preflight loop, the drain / repave / restore sequence with `--dry-run`, the verification checklist, what to do with a `FAILED` workspace, accepted noise, and the post-cutover cleanup. `next_deploy.md` carries the cutover order and the archive-deletion cleanup line.

- `docs/deploy/host-pool-setup.md` describes the gen-2 disk layout and the measured-partition disk budget (64 GiB reserve); `docs/deploy/reference/workspace-stop-start.md` documents `minds-admin workspaces start`.
