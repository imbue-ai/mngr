// Unit tests for how the shell trusts the local forward proxy's TLS: only a
// leaf the proxy's own CA signed, on a loopback origin, and only where
// Chromium's sole objection is that it does not know that CA.
//
// The fixtures are a CA, a loopback leaf it signed, and an unrelated CA, all
// minted with the proxy's own certificate helpers (imbue.mngr_forward.tls).
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const {
  NET_ERR_CERT_AUTHORITY_INVALID,
  NET_OK,
  VERDICT,
  createCertificateVerifyProc,
  decideCertificateVerdict,
  loadForwardProxyCa,
} = require('../../electron/forward-proxy-trust');

const FIXTURES = path.join(__dirname, 'fixtures', 'forward-proxy-trust');
const CA = loadForwardProxyCa(path.join(FIXTURES, 'ca.pem'));
const OTHER_CA = loadForwardProxyCa(path.join(FIXTURES, 'other-ca.pem'));
const LEAF_PEM = fs.readFileSync(path.join(FIXTURES, 'leaf.pem'), 'utf8');
const NET_ERR_CERT_COMMON_NAME_INVALID = -200;

function verdict(overrides = {}, ca = CA) {
  return decideCertificateVerdict(
    { hostname: 'agent-0a1b2c3d4e5f.localhost', errorCode: NET_ERR_CERT_AUTHORITY_INVALID, certificatePem: LEAF_PEM, ...overrides },
    ca,
  );
}

test('a loopback leaf the proxy CA signed is trusted when Chromium only lacks the CA', () => {
  assert.equal(verdict(), VERDICT.TRUST);
  assert.equal(verdict({ hostname: 'localhost' }), VERDICT.TRUST);
  assert.equal(verdict({ hostname: '127.0.0.1' }), VERDICT.TRUST);
  assert.equal(verdict({ errorCode: NET_OK }), VERDICT.TRUST);
});

test('a leaf another CA signed is left to Chromium, which does not know that CA either', () => {
  assert.equal(verdict({}, OTHER_CA), VERDICT.DEFER_TO_CHROMIUM);
});

test('any objection of Chromium beyond the unknown CA stands, name mismatch included', () => {
  assert.equal(verdict({ errorCode: NET_ERR_CERT_COMMON_NAME_INVALID }), VERDICT.DEFER_TO_CHROMIUM);
});

test('an origin that is not loopback is never the proxy, whoever signed its leaf', () => {
  assert.equal(verdict({ hostname: 'example.com' }), VERDICT.DEFER_TO_CHROMIUM);
  assert.equal(verdict({ hostname: 'localhost.example.com' }), VERDICT.DEFER_TO_CHROMIUM);
});

test('a certificate that does not parse is left to Chromium', () => {
  assert.equal(verdict({ certificatePem: 'not a certificate' }), VERDICT.DEFER_TO_CHROMIUM);
});

test('the verify proc answers from the request fields Electron passes', () => {
  const answers = [];
  const logged = [];
  const proc = createCertificateVerifyProc(CA, (line) => logged.push(line));
  proc(
    { hostname: 'agent-0a1b2c3d4e5f.localhost', errorCode: NET_ERR_CERT_AUTHORITY_INVALID, certificate: { data: LEAF_PEM } },
    (code) => answers.push(code),
  );
  proc({ hostname: 'example.com', errorCode: NET_OK, certificate: { data: LEAF_PEM } }, (code) => answers.push(code));
  assert.deepEqual(answers, [VERDICT.TRUST, VERDICT.DEFER_TO_CHROMIUM]);
  assert.deepEqual(logged, []);
});

test('the verify proc reports a loopback origin it leaves to Chromium, with what Chromium objected to', () => {
  const logged = [];
  const proc = createCertificateVerifyProc(CA, (line) => logged.push(line));
  proc(
    {
      hostname: 'svc.agent-0a1b2c3d4e5f.localhost',
      errorCode: -207,
      verificationResult: 'net::ERR_CERT_INVALID',
      certificate: { data: LEAF_PEM },
    },
    () => {},
  );
  assert.equal(logged.length, 1);
  assert.match(logged[0], /svc\.agent-0a1b2c3d4e5f\.localhost/);
  assert.match(logged[0], /errorCode=-207/);
  assert.match(logged[0], /verificationResult=net::ERR_CERT_INVALID/);
  assert.match(logged[0], /issuedByProxyCa=true/);
});
