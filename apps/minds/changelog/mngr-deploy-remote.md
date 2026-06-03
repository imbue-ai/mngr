Fixed several bugs in `minds env deploy` / `recover` and the workspace-create
flow, surfaced while standing up a fresh dev environment:

- Deploy now pushes the `ovh` per-env Modal Secret. The remote-service-connector
  app references `ovh-<tier>-<deploy_id>` via `Secret.from_name` (its release
  route + cleanup cron sign OVH API calls at runtime), but the `ovh` entry was
  missing from every tier's `deploy.toml` `[secrets].services` list, so
  `modal deploy rsc-<tier>` failed with "Secret ... not found in environment".
  Added `ovh` to the dev/staging/production/ci lists and added a regression test
  asserting each tier's `secrets.services` matches `per_env_secret_services()`.
- `minds env recover` now runs non-interactively. The Modal app-stop step ran
  `modal app stop` without `-y`, which aborts with "no interactive terminal
  detected" whenever recover runs without a TTY (auto-rollback after a failed
  deploy, CI, background runs). Added `-y`.
- `minds env recover` is now re-runnable. The Neon instant-restore step was not
  idempotent: a recover that failed a later step left the pre-restore preserve
  branch behind, so re-running returned 409 ("branch with that name already
  exists") and could never delete its recover-target file. The restore now
  treats that 409 as "already restored" and proceeds.
- `minds env deploy` now exits non-zero when a failed deploy rolls back. The
  failure path execs into `minds env recover`, which inherits the exit code; a
  successful rollback therefore reported the *failed* deploy as success (exit 0),
  masking it from callers / CI. `recover` gained a hidden `--from-failed-deploy`
  flag (passed only by that auto-rollback exec) that forces a non-zero exit even
  when the rollback itself succeeds.
- `minds env activate` no longer dead-locks the recover flow. The blanket
  "refuse activation while ANY recover-target file exists" guard created a
  catch-22: `minds env recover` requires an activated env, but activation was
  blocked by the failed env's own recover-target -- so you could never activate
  the env to recover it. Activation now allows activating an env that has its
  own pending recover-target (surfacing any *other* envs' targets as a warning),
  and only hard-refuses when the pending target(s) belong solely to other envs.
- Fixed a `ty` error / runtime breakage in workspace creation from a bad merge:
  `_MngrCreateAttemptParams` still carried a `gh_token` field (and passed it to
  `run_mngr_create`) after `GH_TOKEN` had been removed end-to-end as unused, so
  the param no longer matched `run_mngr_create`'s signature and the field was
  never supplied at the construction site. Removed the leftover `gh_token`.
- Fixed the imbue_cloud fast->slow path fallback. minds decided whether to fall
  back from `fast_mode=require` by substring-matching `"FastPathUnavailableError"`
  in `mngr create`'s output, but mngr surfaces that error as a clean
  `Error: <message>` with no class name -- so the marker never matched and the
  create failed instead of falling back to the slow (rebuild) path. minds now
  parses the structured `{"event":"error","error_class":...}` JSONL record (see
  the mngr-side change), threading `error_class` through `_CreateEventCapture` ->
  `MngrCommandError` and branching on it in `_create_imbue_cloud_with_fallback`.

Also resolved a `runtime/secrets` path collision that broke Cloudflare tunnel
sharing whenever host backups were configured:

- `runtime/secrets` is now consistently a *directory* of per-secret `*.env`
  files inside the workspace, rather than a single shared file. Host backups
  already wrote `runtime/secrets/restic.env` (forcing the directory form),
  which broke the Cloudflare tunnel runner (it read `runtime/secrets` as a
  file and crashed with `IsADirectoryError`) and the Telegram injector (it
  appended to `runtime/secrets`, which fails against a directory).
- The Cloudflare tunnel token now lives at
  `runtime/secrets/cloudflare_tunnel.env`; `inject_tunnel_token_into_agent`
  writes that file (overwrite in place, no more line-strip dance).
- Added `clear_tunnel_token_from_agent`, called from the workspace
  disassociation handler after the tunnel is deleted, so the agent's
  cloudflare-tunnel service stops `cloudflared` instead of spinning against a
  now-deleted tunnel. Previously nothing ever cleared the token.
- The Telegram bot token now lives at `runtime/secrets/telegram.env`
  (overwrite in place) so it no longer collides with the other secrets.

Dev tooling: the minds desktop client launchers now pin Node automatically.

- Added `apps/minds/scripts/select_node_version.sh`, a sourced helper that
  selects the Node version pinned in `apps/minds/.nvmrc` (via nvm) before
  launching the client, so pnpm/npm's `engine-strict` check passes regardless
  of the shell's default Node. It's a no-op when the active Node already
  matches, and errors with an actionable hint (e.g. `nvm install <version>`)
  rather than auto-installing.
- `apps/minds/scripts/propagate_changes` now sources that helper before
  restarting the desktop client (`electron_start`), so the iteration loop no
  longer fails with `ERR_PNPM_UNSUPPORTED_ENGINE` when the shell's Node has
  drifted off the pin.

`minds pool destroy` now does a full teardown: it injects the activated tier's
OVH credentials from Vault (like `minds pool create`) and forwards to the admin
command, which cancels the OVH VPS before dropping the row -- so destroying a
pool host can no longer leave a stranded, still-billing VPS. Pass
`--skip-vps-cancel` only when the VPS is already gone.

Vault reads now distinguish "secret absent" from a transient failure. Added
`VaultSecretNotFoundError` (raised when the Vault CLI exits 2 / "No value
found"); `minds env deploy`'s optional-OVH-entry fallback now catches only that,
so a transient/auth Vault error no longer gets silently turned into empty OVH
credentials (which would deploy a broken `ovh` Modal Secret on a Vault blip).

Fixed a slow-path create failure on shared tiers (staging / production). The
create form there defaults to the remote FCT URL, which minds shallow-cloned;
the imbue_cloud slow path then transfers the clone to the leased host via mngr's
git-mirror push, which git rejects for shallow history ("shallow update not
allowed") -- so any create that fell back to the slow path (no fast/adopt match)
failed outright. minds now full-clones the remote URL for imbue_cloud
(`_may_shallow_clone_remote_repo`), mirroring the local-worktree branch that
already full-cloned for the same reason.

The sharing editor now waits for Cloudflare Access to go live before showing the
URL as ready. After enabling sharing, Cloudflare can take a few seconds to
publish the Access application at the edge; until then the hostname does not
return the Access login redirect, so the link looked broken. The editor now
shows a brief "Provisioning share..." state and polls a new desktop-client
endpoint (`GET /api/sharing-readiness/{agent_id}/{service_name}?url=...`) that
probes the hostname for the Access 302. It reveals the link as soon as the edge
is live, or after a short client-side timeout with a "may take a moment to
become reachable" note. Probing happens in minds (not the connector), so the
connector request stays short and the browser drives the wait.
