# Changelog - modal_litellm

A concise, human-friendly summary of changes for the `modal_litellm` app. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: LiteLLM-proxy deploys now run a Prisma schema push against the proxy's `DATABASE_URL` automatically (via a new `migrate_db` Modal Function invoked by `minds env deploy`), so a fresh tier or dev env no longer requires a manual `prisma db push` step.
- Added: Configurable `scaledown_window` on the LiteLLM proxy Modal function, driven by `MINDS_LITELLM_PROXY_SCALEDOWN_WINDOW` (from the tier's `[scaledown_window].litellm_proxy` in `deploy.toml`). `0` (default) keeps Modal's own default; dev tiers set it high (~10 min) so the no-warm-pool proxy stays hot across a dev session.
- Added: Expanded the supported model list to cover the full current Anthropic Claude lineup: added `claude-opus-4-8` (latest Opus), `claude-opus-4-6`, `claude-opus-4-5`, `claude-opus-4-1`, `claude-sonnet-4-5`, and the bare `claude-haiku-4-5` alias, alongside the existing `claude-opus-4-7`, `claude-sonnet-4-6`, and dated Opus 4 / Sonnet 4 / Haiku 4.5 ids. Each model carries inline per-token pricing (input, output, cache-write, cache-read) via `litellm_params`, so cost tracking stays accurate even on litellm versions whose bundled price map predates a model. New `config_drift_test.py` fails CI if `app.py`'s model list and the local-dev `litellm_proxy/config.yaml` ever diverge.
