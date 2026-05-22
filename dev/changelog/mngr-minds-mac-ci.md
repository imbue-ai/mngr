Add `.github/workflows/minds-mac-smoke.yml`: a GitHub Actions workflow
that smoke-tests the minds desktop app on a hosted macOS runner. It
installs the app, launches Electron, and verifies the backend comes up
and the landing page and create form render (`integ_check.py
--launch-only`, with `SKIP_AUTH=1`). It stops short of creating an agent
-- hosted macOS runners have no nested virtualization, so the Lima VM an
agent needs cannot boot there. Trigger via the Actions tab or
`gh workflow run minds-mac-smoke.yml`.
