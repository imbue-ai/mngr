// Unit tests for the environment shared by every bundled-uv spawn.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { BUNDLED_UV_ENV } = require('../../electron/bundled-uv-env');

test('bundled uv is told to trust the OS certificate store', () => {
  // Without this, uv verifies TLS only against its bundled Mozilla roots, so a
  // network that re-signs HTTPS with a CA trusted by the OS fails env setup
  // with `invalid peer certificate: UnknownIssuer`.
  assert.equal(BUNDLED_UV_ENV.UV_SYSTEM_CERTS, '1');
});

test('env setup hands the shared env to the uv it spawns', async (t) => {
  // A stand-in uv that reports the variable and fails, so runEnvSetup rejects
  // with the report in its error. `./paths` is replaced in the require cache
  // because the real one imports electron.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'minds-uv-env-'));
  const fakeUv = path.join(dir, 'uv');
  fs.writeFileSync(fakeUv, '#!/bin/sh\necho "UV_SYSTEM_CERTS=${UV_SYSTEM_CERTS:-unset}" >&2\nexit 2\n', { mode: 0o755 });
  const pathsModule = require.resolve('../../electron/paths');
  const envSetupModule = require.resolve('../../electron/env-setup');
  require.cache[pathsModule] = {
    id: pathsModule,
    filename: pathsModule,
    loaded: true,
    exports: {
      isDev: () => false,
      getUvPath: () => fakeUv,
      getPyprojectDir: () => dir,
      getVenvDir: () => path.join(dir, 'venv'),
      getUvCacheDir: () => path.join(dir, 'cache'),
      getUvPythonDir: () => path.join(dir, 'python'),
      getLogDir: () => path.join(dir, 'logs'),
      getUvShimBinDir: () => path.join(dir, 'shims'),
    },
  };
  t.after(() => {
    delete require.cache[pathsModule];
    delete require.cache[envSetupModule];
    fs.rmSync(dir, { recursive: true, force: true });
  });

  const { runEnvSetup } = require('../../electron/env-setup');
  await assert.rejects(runEnvSetup(() => {}), /UV_SYSTEM_CERTS=1/);
});
