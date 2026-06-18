# Unabridged Changelog - modal_litellm

Full, unedited changelog entries consolidated nightly from individual files in `apps/modal_litellm/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-16

Added a drift test (`mngr_usage_pricing_drift_test.py`) asserting that every Anthropic model this app prices inline also exists in `mngr_usage`'s token-pricing table with identical per-token prices. `mngr_usage` mirrors these numbers verbatim (it can't share them by import -- this app deploys into a Modal image with none of the imbue packages), so the test makes that mirror enforceable: changing an Anthropic price on either side without the other now fails CI. No runtime change to the app.

## 2026-06-05

Expanded the LiteLLM proxy's supported model list to cover the full current Anthropic Claude lineup: added `claude-opus-4-8` (latest Opus), `claude-opus-4-6`, `claude-opus-4-5`, `claude-opus-4-1`, `claude-sonnet-4-5`, and the bare `claude-haiku-4-5` alias, alongside the previously supported `claude-opus-4-7`, `claude-sonnet-4-6`, and the dated Opus 4 / Sonnet 4 / Haiku 4.5 ids. Each model now carries inline per-token pricing (input, output, cache-write, cache-read) registered via `litellm_params`, mirrored from litellm's price map, so cost tracking is accurate even on litellm versions whose bundled price map predates a model. Added `config_drift_test.py`, which fails CI if `app.py`'s model list and the local-dev `litellm_proxy/config.yaml` ever diverge.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-03

Added a configurable `scaledown_window` to the LiteLLM proxy Modal function, driven by `MINDS_LITELLM_PROXY_SCALEDOWN_WINDOW` (from the tier's `[scaledown_window].litellm_proxy` in `deploy.toml`). `0` (default) keeps Modal's own default; dev tiers set it high (~10 min) so the no-warm-pool proxy stays hot across a dev session.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

- ``modal_litellm``'s README + module docstring drop the wrong
  ``/anthropic`` suffix from the documented ``ANTHROPIC_BASE_URL`` --
  the Anthropic SDK appends ``/v1/messages`` itself, which lands on
  LiteLLM's native route that already accepts the Anthropic request
  shape. (F1)

LiteLLM-proxy deploys now run a Prisma schema push against the proxy's DATABASE_URL automatically (via a new `migrate_db` Modal Function invoked by `minds env deploy`), so a fresh tier or dev env no longer requires a manual `prisma db push` step before the first virtual-key create.
