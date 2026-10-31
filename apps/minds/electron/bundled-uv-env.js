'use strict';

// Environment shared by every spawn of the bundled uv (the env-setup `uv sync`
// and the backend's `uv run`), either of which may download the managed
// CPython from GitHub and wheels from PyPI.
//
// Free of any `electron` import so it is unit-testable under plain node (see
// ../test/unit/bundled-uv-env.test.js).

const BUNDLED_UV_ENV = Object.freeze({
  // By default uv verifies TLS against its own bundled Mozilla root set, not
  // the OS trust store; this makes it load the platform's native certificates
  // instead.
  UV_SYSTEM_CERTS: '1',
});

module.exports = { BUNDLED_UV_ENV };
