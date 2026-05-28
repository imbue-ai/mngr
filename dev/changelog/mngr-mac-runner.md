# Self-hosted Mac runner + smoke workflow

- Added `.github/workflows/mac-runner-smoke.yml`, a minimal `workflow_dispatch`-triggered job that targets the new `minds-runner`-labeled self-hosted macOS runner and prints diagnostic info (hostname, Tailscale IP, etc.).
- Companion infrastructure (the runner Mac itself: Tailscale-tagged, LaunchAgent-installed GitHub Actions runner) lives outside this repo. The runner is registered at the `imbue-ai` org level and can be targeted by any repo via `runs-on: [self-hosted, macOS, minds-runner]`.
- Intent: serve as the foundation for an upcoming Minds "launch to first message" verification job. This PR only lands the smoke test to prove the pipeline lights up end-to-end.
