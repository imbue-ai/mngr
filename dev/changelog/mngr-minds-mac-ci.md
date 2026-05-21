Add `.github/workflows/minds-mac-smoke.yml`: a manually-triggered GitHub
Actions workflow that runs the minds desktop-app smoke test on a macOS
runner. The `dev-source-smoke` job launches Electron from `apps/minds`
source and runs `integ_check.py` (create a Lima agent from
forever-claude-template, assert the chat UI mounts). The optional
`packaged-smoke` job installs a published minds.app and drives it via
`drive-minds.sh`. Trigger with `gh workflow run minds-mac-smoke.yml`.
