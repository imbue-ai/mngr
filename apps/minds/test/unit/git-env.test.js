// Unit tests for the bundled git's environment and the CA bundle it trusts.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { certifiBundlePathFor, bundledGitEnv } = require('../../electron/git-env');

const PEM_TEXT = '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n';

/** A venv laid out the way uv writes one: pyvenv.cfg plus a POSIX lib/pythonX.Y tree. */
function makeVenv(t, { versionInfo, withCertifi }) {
  const venvDir = fs.mkdtempSync(path.join(os.tmpdir(), 'git-env-test-'));
  t.after(() => fs.rmSync(venvDir, { recursive: true, force: true }));
  fs.writeFileSync(
    path.join(venvDir, 'pyvenv.cfg'),
    `home = /opt/python/bin\nimplementation = CPython\nuv = 0.13.4\nversion_info = ${versionInfo}\n` +
      'include-system-site-packages = false\nprompt = minds-desktop\n'
  );
  const [major, minor] = versionInfo.split('.');
  const certifiDir = path.join(venvDir, 'lib', `python${major}.${minor}`, 'site-packages', 'certifi');
  fs.mkdirSync(certifiDir, { recursive: true });
  if (withCertifi) {
    fs.writeFileSync(path.join(certifiDir, 'cacert.pem'), PEM_TEXT);
  }
  return venvDir;
}

test('git verifies TLS against the venv certifi bundle', (t) => {
  const venvDir = makeVenv(t, { versionInfo: '3.12.13', withCertifi: true });

  const env = bundledGitEnv({ gitRoot: '/opt/app/resources/git', caBundlePath: certifiBundlePathFor(venvDir) });

  assert.equal(env.GIT_SSL_CAINFO, path.join(venvDir, 'lib', 'python3.12', 'site-packages', 'certifi', 'cacert.pem'));
  assert.equal(fs.readFileSync(env.GIT_SSL_CAINFO, 'utf8'), PEM_TEXT);
  assert.equal(env.GIT_EXEC_PATH, '/opt/app/resources/git/libexec/git-core');
});

test('the site-packages directory follows the interpreter the venv records', (t) => {
  const venvDir = makeVenv(t, { versionInfo: '3.13.2', withCertifi: true });

  assert.equal(
    certifiBundlePathFor(venvDir),
    path.join(venvDir, 'lib', 'python3.13', 'site-packages', 'certifi', 'cacert.pem')
  );
});

test('a venv without certifi fails loudly, naming the missing pem', (t) => {
  const venvDir = makeVenv(t, { versionInfo: '3.12.13', withCertifi: false });

  assert.throws(() => certifiBundlePathFor(venvDir), (err) => {
    assert.match(err.message, /certifi CA bundle/);
    assert.ok(err.message.includes(path.join(venvDir, 'lib', 'python3.12', 'site-packages', 'certifi', 'cacert.pem')));
    return true;
  });
});

test('a missing venv fails loudly instead of leaving git on another trust store', (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'git-env-test-'));
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));

  assert.throws(() => certifiBundlePathFor(path.join(parent, '.venv')), /no virtualenv at/);
});
