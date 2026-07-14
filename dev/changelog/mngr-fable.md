- Bump the pinned Claude Code CLI version from 2.1.160 to 2.1.207 in CI workflows (`release-tests.yml`, `tmr-setup` action) and the minds e2e snapshot script, matching the new workspace pin that supports Claude Fable 5.

- Add `claude-fable-5` with inline pricing ($10 / $50 per 1M input / output tokens, cache write 1.25e-5, cache read 1e-6) to the repo-root local-dev LiteLLM proxy config (`litellm_proxy/config.yaml`), kept in sync with `apps/modal_litellm/app.py` by a drift test.
