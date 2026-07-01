Migrated the GitHub Actions workflows off GitHub-stored secrets and onto HashiCorp Vault (via the `imbue-ai/use-vault-secrets` OIDC action), so CI credentials are managed centrally in Vault instead of the repo's Actions settings.

- CI test/TMR jobs (`ci.yml`, `vet.yml`, `release-tests.yml`, `tmr.yml`, `tmr-reintegrate.yml`) now fetch the Anthropic key, imbue Modal workspace token (both id and secret), and the TMR S3 credentials from Vault under `mngr/ci/*`, using a new repo-bound `mngr_ci_gh` role. The `MODAL_TOKEN_ID` repo variable and the `ANTHROPIC_API_KEY` / `MODAL_TOKEN_SECRET` / `AWS_*` Actions secrets are no longer used.

- The minds CI-env jobs in `ci.yml` now read the minds-dev Modal token from Vault (`minds/ci/modal/*`) via their existing Vault login, replacing the `MINDS_DEV_MODAL_TOKEN_*` variable/secret.

- The `minds-launch-to-msg.yml` build job fetches its ToDesktop signing credentials from a separate, environment-gated release path (`mngr/release/*`, role `mngr_release_gh`, GitHub Environment `minds-release`); its launch and Slack-notify jobs read the Anthropic key and Slack webhook from `mngr/ci/*`.

- The automatic per-run `GITHUB_TOKEN` (used for same-repo `git push` / `gh` calls and check-run reporting) is intentionally left as-is -- it is not a stored secret and cannot meaningfully live in Vault.

- `scripts/changelog_deploy.sh` now reads its bot token from `secrets/mngr/dev/GH_TOKEN` (the developer-direct-access path) and its Anthropic key from the shared `secrets/mngr/ci/ANTHROPIC_API_KEY`, replacing the old `mngr/dev/github` and `mngr/dev/anthropic` paths.

The Vault roles/policies backing these paths are defined in the separate `imbue-ai/vault` Terraform repo. Note: the self-hosted macOS `minds-runner` must have `curl` and `jq` on PATH for the Vault action to run.
