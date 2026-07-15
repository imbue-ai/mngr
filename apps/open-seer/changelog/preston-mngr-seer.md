Add the open-seer app: an autonomous Sentry-error-to-PR system. It polls Sentry for new issues, triages them, and drives Claude Code sessions (via the fix-sentry-error and sentry-sweep skills) to produce candidate fix PRs. Includes the tick loop, a two-scanner secrets/PII gate (Betterleaks with custom PII rules and Kingfisher — both must pass before a fixer's PR goes up), Dockerfile, design docs, and tests.

The fixer skill also triages its PR's CI checks before flipping ready: failures caused by the diff are fixed (up to 3 cycles, then escalation), while pre-existing/infrastructure failures (e.g. Vault-gated jobs that can only authenticate from the canonical repo, not the mirror) are documented on the PR with evidence instead of chased.

The tick module is `tick.py` (renamed from `app.py`, which collided with another workspace member's module of the same name); deploy with `modal deploy tick.py`. The project also now carries the standard monorepo scaffolding: changelog layout, ratchet tests, coverage config, and wheel excludes.

Fixer PRs include a "Regression risk" section: a low/medium/high rating with evidence (blast radius, off-path behavior changes, what protects against a regression, and the riskiest assumption for the reviewer to check).

Agent spawns (the tick's sweep and the sweep's fixers) are now robust on fresh namespaces and non-modal providers: they pass an explicit --type claude, set agent_types.claude.auto_allow_permissions/auto_dismiss_dialogs so unattended agents never hang at a permission dialog, and only send the modal-specific sandbox-timeout build arg to the modal provider (docker rejects it). The tick also provisions mngr onto the sweep's host when the image lacks it, so the sweep can spawn fixers.

The assignment marker is now SENTRY_ASSIGNEE (a user email or team:<slug>) instead of the team-only SENTRY_TEAM, and the sweep fails fast when a configured team does not resolve.

Sweep-host provisioning now installs the full toolchain when the image lacks it (mngr with its claude plugin, gh, and the Betterleaks/Kingfisher scanners), the sweep bootstraps exact dev-built mngr wheels from a workspace wheels/ directory when present (the PyPI release can lag the skills), and the fixer provider is configurable via OPEN_SEER_FIXER_PROVIDER (default modal; "local" runs fixers on the sweep's host for tests without Docker-in-Docker or Modal credentials).

The agents can now authenticate Claude Code with a Claude Code OAuth token (`claude setup-token`) in addition to an API key: `CLAUDE_CODE_OAUTH_TOKEN` is forwarded from the tick to the sweep and on to each fixer host alongside `ANTHROPIC_API_KEY`. Set either (or both) in the `open-seer` Modal secret / `.env`; Claude Code prefers the OAuth token when both are present.
