# slack mock for CI

`slack-mock-server.js` is a localhost HTTP server mimicking the subset
of slack.com used by `drive-slack.js`. Listens on port 8443 by default.

## Endpoints

| Method | Path | Response shape |
|---|---|---|
| GET | `/api/auth.test` | `{ok:true, url, user, team, team_id, user_id}` |
| GET | `/api/conversations.list` | `{ok:true, channels:[{id, name, ...}], response_metadata}` |
| GET | `/api/conversations.history` | `{ok:true, messages:[{user, text, ts, username}], has_more, response_metadata}` |
| POST | `/api/oauth/v2/access` | `{ok:true, access_token, scope, team, authed_user}` |
| any | (other) | `{ok:false, error:'mock_unimplemented_endpoint'}` |

The canned channel name is `ci-mock-channel`, the canned sender is
`Mock Sender`, and the canned message text is the constant
`MESSAGE_BODY` in the server source (`CI MOCK: greetings from the
localhost slack mock.`). The test asserts this exact string lands in
the assistant's chat reply.

## CI wiring (planned, follow-up PR)

The self-hosted MacBook `minds-runner` job in `minds-launch-to-msg.yml`
will get a new step set:

```yaml
- name: stand up slack mock + hosts redirect
  run: |
    # Plain HTTP for the mock; socat terminates TLS for the agent's
    # HTTPS curl call to slack.com.
    node apps/minds/test/e2e/mocks/slack-mock-server.js &
    MOCK_PID=$!
    echo "MOCK_PID=$MOCK_PID" >> $GITHUB_ENV
    sleep 1
    curl -sf http://127.0.0.1:8443/api/auth.test >/dev/null
    # generate a self-signed cert for slack.com
    openssl req -x509 -newkey rsa:2048 -nodes -keyout /tmp/key.pem \
      -out /tmp/cert.pem -subj "/CN=slack.com" -days 1
    # trust it at the user keychain level (no Gatekeeper prompt; runner
    # is non-interactive so we set the trust setting via security.)
    sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain /tmp/cert.pem
    # socat: TLS terminate on 443 -> plain HTTP 8443
    sudo socat -d -d \
      OPENSSL-LISTEN:443,reuseaddr,fork,cert=/tmp/cert.pem,key=/tmp/key.pem,verify=0 \
      TCP:127.0.0.1:8443 &
    echo "SOCAT_PID=$!" >> $GITHUB_ENV
    # /etc/hosts redirect slack.com -> 127.0.0.1
    echo "127.0.0.1 slack.com files.slack.com" | sudo tee -a /etc/hosts

- name: pre-seed latchkey slack creds
  run: |
    ~/.minds/.venv/bin/latchkey auth set slack \
      -H "Authorization: Bearer mock-tok" \
      -H "Cookie: d=mock-d"

- name: drive slack flow
  env:
    MINDS_WORKSPACE: <from previous step>
  run: |
    cd apps/minds
    node test/e2e/drive-slack.js
    # asserts the reply contains "Mock Sender" and the canned MESSAGE_BODY

- name: teardown
  if: always()
  run: |
    [[ -n "$MOCK_PID" ]] && kill $MOCK_PID || true
    [[ -n "$SOCAT_PID" ]] && sudo kill $SOCAT_PID || true
    sudo sed -i.bak '/^127\.0\.0\.1 slack\.com files\.slack\.com$/d' /etc/hosts
    sudo security remove-trusted-cert -d /tmp/cert.pem || true
```

## Local manual run

```bash
# Terminal 1 -- start the mock
node test/e2e/mocks/slack-mock-server.js

# Terminal 2 -- smoke test
curl -s http://127.0.0.1:8443/api/auth.test | jq
curl -s http://127.0.0.1:8443/api/conversations.list | jq

# To exercise the full agent path locally without changing /etc/hosts,
# you'd need to either:
#   (a) edit the bundled latchkey slack.js baseApiUrls (will break code
#       signature; minds.app may refuse to launch via Gatekeeper)
#   (b) register a mock-slack service via latchkey services register
#       and have the agent's prompt direct it at slack-mock (changes
#       the workflow shape)
#   (c) do the full /etc/hosts + socat dance (intrusive on a dev machine)
#
# Easiest for local dev: just use the real slack creds on a real workspace,
# as the user already does (`drive-slack.js` against weishi30 works).
# The mock exists for the CI path where no real creds are available.
```
