# Playwright E2E in CI — design notes

Goal: run `macos-launch.spec.js`, `drive-existing.js`-equivalent, and a
slack-permission-flow scenario in CI, driven by Playwright clicks.

## Two CI workflows (split by runner capability)

### `minds-macos-launch.yml` — `runs-on: macos-latest`

- Free GitHub-hosted M-series Mac runners (no nested virt; can't boot lima).
- Scope:
  - `macos-launch.spec.js` — `/Applications/Minds.app` launches, Create
    link visible, projects landing renders.
  - `headed-demo.spec.js` (sans `--headed`) — Create form fills cleanly.
- Triggers: every push to `wz/minds_onboard` (and PRs).
- Provides cold-launch coverage on a vanilla macOS image. Catches the
  `_MNGR_FORWARD_LISTEN_TIMEOUT_SECONDS` class of regression (Tart
  caught the 5s-deadline bug for us; this workflow replaces Tart-as-CI
  with a free hosted-runner equivalent).

### `minds-launch-to-msg.yml` (existing) — `runs-on: [self-hosted, macOS, minds-runner]`

- Self-hosted MacBook; has lima + nested virt.
- Extend the verify job to ALSO run:
  - `drive-existing.js` equivalent — full home -> click workspace ->
    chat round-trip with a nonce-verified reply.
  - `drive-slack.js` — slack permission flow with mock Slack API
    (see below).
- Existing job's `first-message-verify.sh` stays for now as a
  belt-and-braces lower-level check.

## Slack mock design (the harder part)

Latchkey's `slack.js` registers `baseApiUrls = ['https://slack.com/api/',
'https://files.slack.com/']` and `loginUrl = 'https://slack.com/signin'`.
These are hardcoded class fields. No env-var override and no public
config knob.

The agent calls `latchkey curl https://slack.com/api/...` and latchkey
injects auth headers + spawns real curl. So the request goes to whatever
DNS resolves `slack.com` to.

### Three viable mock-integration paths

| Path | Mechanism | Pros | Cons |
|---|---|---|---|
| 1. /etc/hosts + TLS | Add `127.0.0.1 slack.com files.slack.com` to /etc/hosts on the CI runner; serve HTTPS with a self-signed cert installed in the user keychain | No bundle changes; standard pattern | sudo writes to /etc/hosts; cert install needs `security add-trusted-cert`; affects the runner globally for the job's lifetime |
| 2. Patch bundled slack.js | At test setup, edit `/Applications/Minds.app/Contents/Resources/latchkey/.../slack.js` to change `baseApiUrls` to `http://localhost:7777/api/` | Per-test scoped; no DNS or TLS | Breaks bundle code signature; on a clean install Gatekeeper may refuse the launch even after `xattr -dr com.apple.quarantine`. Needs ad-hoc resign and may bite us. |
| 3. Register mock service | Run `latchkey services register slack-mock --base-url http://localhost:7777/api/ --family slack`; teach the agent to call `slack-mock` | No DNS/cert/bundle changes | Agent doesn't auto-discover services; would need a workspace settings.toml or a prompt change to direct the agent at `slack-mock`. Doesn't exercise the user's actual slack tool-call path. |

### Recommended: path 1 with a twist

Skip the macOS keychain dance by using **HTTP (not HTTPS) on the mock
server** and patching latchkey's `slack.js::baseApiUrls` at runtime in
the venv-extracted wheel (NOT in the bundled .asar — that's a no-op for
node code, but our pyproject doesn't touch latchkey; latchkey is in
node_modules under Resources/latchkey/).

Wait. latchkey is a node package; minds.app spawns it as a subprocess.
The node code lives in
`/Applications/Minds.app/Contents/Resources/latchkey/node_modules/latchkey/dist/src/services/slack.js`.
That's in the signed bundle, so we can't safely edit it.

So path 1 it is. Concrete plan:

1. Provide a mock server (Node, runs in CI) that listens on `localhost:7777`
   and serves:
   - `GET /api/auth.test` -> `{ok: true, url: ..., team: ..., user: ...}`
   - `GET /api/conversations.list` -> canned channel list
   - `GET /api/conversations.history?channel=...` -> canned messages
   - `POST /api/oauth/v2/access` -> canned access token (for browser flow)
2. CI step: `echo "127.0.0.1 slack.com files.slack.com" | sudo tee -a /etc/hosts`
3. CI step: generate a self-signed cert (`mkcert -install` or openssl)
   for slack.com + files.slack.com.
4. CI step: install cert into user keychain
   (`security add-trusted-cert -k ~/Library/Keychains/login.keychain-db ...`).
5. Pre-seed latchkey creds with a known mock token:
   `latchkey auth set slack -H "Authorization: Bearer mock-tok" -H "Cookie: d=mock-d"`.
6. Mock server starts in the background before the Playwright tests.
7. drive-slack.js runs: agent receives prompt, calls slack tool ->
   curl https://slack.com/api/... -> /etc/hosts -> localhost:443 (or 7777
   if the mock binds 443 via setcap-like) -> mock returns canned data.
8. Whether the minds.app permission dialog fires depends on whether
   the workspace's detent policy allows the call without consent.
   On a fresh CI runner with `mac-runner-reset.sh` wiping `~/.minds/`,
   consent is uncached -> dialog should fire -> Playwright clicks
   Approve -> tool call proceeds.

### Open questions

1. Does macOS allow binding to port 443 without root for our mock server?
   Answer: no. Run mock on 8443 and configure latchkey/agent to use
   a non-standard port — OR use `socat` / a sudo'd port-forwarder.
2. Does the slack service URL `https://slack.com/api/` allow a port?
   Answer: yes (`https://slack.com:8443/api/`) — but the slack service's
   `baseApiUrls` regex check happens in latchkey at credential-scrape
   time, not at curl-injection time. The injection-time logic only
   sees the curl command's URL. So we just need the CURL URL to point
   at localhost (via /etc/hosts).
3. Does Slack's API allow a port suffix? In production the prompt would
   say "use slack.com" without a port. With /etc/hosts mapping
   slack.com -> 127.0.0.1 and our mock listening on 443, no port suffix
   is needed in the curl URL. To avoid the port-443-requires-root
   issue, install a port-forwarder (`pfctl rdr-anchor`) or use launchd's
   redirect (`socat TCP-LISTEN:443,reuseaddr,fork TCP:localhost:7777`
   run with sudo at CI setup).

### What ships in this PR

This PR lands the **macOS launch CI workflow only** plus this design
doc. The slack mock and self-hosted runner extension come in a
follow-up PR once the mock-server scaffold is built.
