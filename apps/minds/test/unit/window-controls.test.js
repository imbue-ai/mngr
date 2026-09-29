// Unit tests for the window-controls rule.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');

const windowControls = require('../../electron/window-controls');

test('macOS always has its native traffic lights, whatever the environment says', () => {
  assert.equal(windowControls.resolveWindowControls('darwin', {}), windowControls.NATIVE_MAC);
  assert.equal(
    windowControls.resolveWindowControls('darwin', { MINDS_WINDOW_CONTROLS: 'mac' }),
    windowControls.NATIVE_MAC
  );
});

test('other platforms get the bar buttons unless the environment asks for drawn traffic lights', () => {
  assert.equal(windowControls.resolveWindowControls('linux', {}), windowControls.BUTTONS);
  assert.equal(windowControls.resolveWindowControls('linux', { MINDS_WINDOW_CONTROLS: '' }), windowControls.BUTTONS);
  assert.equal(windowControls.resolveWindowControls('win32', {}), windowControls.BUTTONS);
  assert.equal(
    windowControls.resolveWindowControls('linux', { MINDS_WINDOW_CONTROLS: 'mac' }),
    windowControls.DRAWN_MAC
  );
});

test('a value the variable does not accept is refused rather than read as a platform default', () => {
  assert.throws(
    () => windowControls.resolveWindowControls('linux', { MINDS_WINDOW_CONTROLS: 'osx' }),
    /MINDS_WINDOW_CONTROLS="osx" is not a window style/
  );
});

test('the preload switch carries the resolved controls by name', () => {
  assert.equal(windowControls.windowControlsSwitch(windowControls.DRAWN_MAC), '--minds-window-controls=drawn-mac');
});
