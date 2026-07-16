Added dependency/CVE and secrets scanning (CASA 6.1.1):

- New `.github/dependabot.yml` enabling weekly Dependabot updates for the `uv` Python ecosystem (root `uv.lock`), the `npm` ecosystem for `apps/minds`, and `github-actions`. Minor and patch bumps are grouped per ecosystem to reduce PR noise; the open-PR limit is 5 per ecosystem.

- New `.github/workflows/dependency-audit.yml` scheduled workflow (weekly cron plus `workflow_dispatch`) that runs `pip-audit` against the resolved Python environment and `pnpm audit --prod` for `apps/minds`. The audit steps are informational (non-blocking) and print findings in the job log and step summary.

- Added a `gitleaks` secrets-scanning pre-commit hook (pinned to `v8.30.1`) to `.pre-commit-config.yaml`, which scans staged changes for committed secrets and blocks the commit on a match.
