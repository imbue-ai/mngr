# Install Node.js in the offload sandbox image

- `libs/mngr/imbue/mngr/resources/Dockerfile`: install Node.js (pinned to the
  version `apps/minds/package.json`'s `engines.node` field declares,
  currently `24.15.0`) into the offload sandbox image. Until now no offload
  test needed a JS runtime; the test in `apps/minds/scripts/build_test.py`
  that reads `apps/minds/todesktop.js` (via `node -e
  "console.log(JSON.stringify(require('./todesktop.js')))"`) is the first.
  Direct binary install from nodejs.org -- exact-patch pin, matching the
  project's exact-pin philosophy (`apps/minds/.nvmrc` and the `engines.node`
  field already use the same version). Side effect: two previously-skipped
  Node-dependent tests in `libs/mngr_latchkey/imbue/mngr_latchkey/extensions/`
  (`permission_requests_test.py`, `minds_api_proxy_test.py`) -- which guard
  themselves with `pytestmark = pytest.mark.skipif(_NODE_BINARY is None,
  ...)` -- now actually run in CI instead of skipping.
