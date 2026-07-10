# open-seer

open-seer is an autonomous Sentry-error-to-PR system for the minds app: an hourly tick sweeps `minds-*` Sentry projects, a manager agent groups new errors by root cause, and one fixer agent per root cause reproduces the error and opens a verified, guardian-reviewed PR. All judgment lives in two skills — the only deterministic code is a one-page Modal cron; Sentry and GitHub are the state store.

Full spec: [DESIGN.md](DESIGN.md) · diagram: [docs/architecture.svg](docs/architecture.svg)

## Testing the skills

Mirrors DESIGN.md §10 — the skills are the system, so test them directly first.

Inside the mngr monorepo, everything below runs **from this app directory** (`cd apps/open-seer`): Claude Code loads the project skills from the nearest `.claude/skills/` at your cwd, so sessions started here get `/fix-sentry-error` and `/sentry-sweep` without touching the monorepo root.

1. **Configure:** `cp .env.example .env` and fill in the tokens (`SENTRY_AUTH_TOKEN`, `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`, …).
2. **One fixer, by hand:** run `claude` here and invoke `/fix-sentry-error <sentry issue url>` for a single issue — watch the worktree → repro → draft PR flow.
3. **One sweep, dry then live:** run `/sentry-sweep` with `OPEN_SEER_DRY_RUN=1`, read the printed decisions (spawn/join/already-resolved), then run live against a test project.
4. **The spawned path:** rerun through mngr — `mngr create sweep-test --from :apps/open-seer --message "/sentry-sweep …"` (the `--from :PATH` scopes the agent's workspace to this app dir instead of the monorepo git root, so the skills load) — and confirm fixers spawn on fresh images with the registry keys installed. In the deployed image this scoping is automatic: the workdir is `/opt/open-seer`.
5. **Hourly ticks:** `modal deploy app.py`.

## Debug access (SSH into a fixer)

Every fixer machine is SSH-able by the team; the connect command is posted as a comment on the Sentry issue.

1. Run `uv run --project <path-to-mngr-checkout> python scripts/setup_debug_access.py` to generate/collect your SSH public key (the script imports mngr internals, so it runs in your mngr checkout's environment).
2. PR your key into `.github/open-seer-authorized-keys` (one key per teammate, tmr-style) — it's installed on every fixer image at spawn.
3. Connect: `MNGR_HOST_DIR=~/.mngr-open-seer mngr connect fixer-<short-id>`

## Kill switch

Set `OPEN_SEER_ENABLED=0` (a Modal secret — flip it, no redeploy; nothing new spawns from the next tick). For in-flight agents (mngr has no name globbing — filter with a CEL expression and pipe the ids to destroy):

```bash
export MNGR_HOST_DIR=~/.mngr-open-seer
mngr list --safe --ids --include 'name.startsWith("sweep-") || name.startsWith("fixer-")' \
  | mngr destroy - --force
```
