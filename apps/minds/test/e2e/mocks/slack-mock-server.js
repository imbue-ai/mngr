// Localhost mock for slack.com used by drive-slack.js in CI.
//
// Listens on http://localhost:8443 by default. The CI verify job
// pre-flights /etc/hosts to map slack.com -> 127.0.0.1 and uses
// socat to forward port 443 -> 8443 so the agent's `curl https://slack.com/...`
// hits this mock. HTTPS is terminated at socat (via stunnel or socat
// with cert) -- this mock itself speaks plain HTTP.
//
// Endpoints (slack Web API subset, all return application/json):
//
//   GET  /api/auth.test                 -> {ok:true, url, user, team_id, user_id}
//   GET  /api/conversations.list        -> {ok:true, channels:[...]}
//   GET  /api/conversations.history     -> {ok:true, messages:[...]}
//   POST /api/oauth/v2/access           -> {ok:true, access_token, scope, team}
//
// Canned data is in `canned/` for the test to assert against; the
// content is deterministic so the test reply can be matched exactly.
//
// Usage (locally):
//   node slack-mock-server.js
//   curl -s http://localhost:8443/api/auth.test
//
// Usage (in CI verify job, expanded form):
//   node apps/minds/test/e2e/mocks/slack-mock-server.js &
//   sudo socat -d -d TCP-LISTEN:443,reuseaddr,fork \
//      OPENSSL:localhost:8443,verify=0,cert=cert.pem,key=key.pem &
//   echo "127.0.0.1 slack.com files.slack.com" | sudo tee -a /etc/hosts

const http = require('node:http');
const PORT = parseInt(process.env.SLACK_MOCK_PORT || '8443', 10);

const TEAM_NAME = 'Imbue CI Mock';
const TEAM_ID = 'TMOCK000';
const USER_ID = 'UMOCK001';
const CHANNEL_ID = 'CMOCK000';
const CHANNEL_NAME = 'ci-mock-channel';
const SENDER = 'Mock Sender';
const MESSAGE_BODY = 'CI MOCK: greetings from the localhost slack mock.';

function send(res, status, body) {
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Cache-Control': 'no-store',
  });
  res.end(JSON.stringify(body));
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const path = url.pathname;
  console.log(`[mock] ${req.method} ${path}`);

  if (req.method === 'GET' && path === '/api/auth.test') {
    return send(res, 200, {
      ok: true,
      url: 'https://slack.com/',
      team: TEAM_NAME,
      user: 'mock-bot',
      team_id: TEAM_ID,
      user_id: USER_ID,
    });
  }

  if (req.method === 'GET' && path === '/api/conversations.list') {
    return send(res, 200, {
      ok: true,
      channels: [{
        id: CHANNEL_ID,
        name: CHANNEL_NAME,
        is_channel: true,
        is_member: true,
        is_private: false,
        num_members: 2,
      }],
      response_metadata: { next_cursor: '' },
    });
  }

  if (req.method === 'GET' && path === '/api/conversations.history') {
    return send(res, 200, {
      ok: true,
      messages: [{
        type: 'message',
        user: USER_ID,
        text: MESSAGE_BODY,
        ts: '1717085000.000100',
        username: SENDER,
      }],
      has_more: false,
      response_metadata: { next_cursor: '' },
    });
  }

  if (req.method === 'POST' && path === '/api/oauth/v2/access') {
    return send(res, 200, {
      ok: true,
      access_token: 'xoxc-mock-token-for-ci-only',
      scope: 'channels:history,channels:read',
      team: { id: TEAM_ID, name: TEAM_NAME },
      authed_user: { id: USER_ID, scope: 'identify', access_token: 'xoxp-mock-user-token' },
    });
  }

  // Default: 404 with slack-style error shape so latchkey doesn't choke.
  send(res, 404, { ok: false, error: 'mock_unimplemented_endpoint', path });
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[mock] slack-mock listening on http://127.0.0.1:${PORT}`);
  console.log(`[mock] canned message: "${MESSAGE_BODY}"`);
});

process.on('SIGINT', () => server.close(() => process.exit(0)));
process.on('SIGTERM', () => server.close(() => process.exit(0)));
