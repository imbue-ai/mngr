// Unit tests for the window-controls rule.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');

const windowControls = require('../../electron/window-controls');

const LINUX_RELEASE = '6.8.0-49-generic';
const WSL2_RELEASE = '5.15.167.4-microsoft-standard-WSL2';

test('macOS always has its native traffic lights, whatever the environment says', () => {
  assert.equal(windowControls.resolveWindowControls('darwin', {}, '25.3.0'), windowControls.NATIVE_MAC);
  assert.equal(
    windowControls.resolveWindowControls('darwin', { MINDS_WINDOW_CONTROLS: 'mac' }, '25.3.0'),
    windowControls.NATIVE_MAC
  );
});

test('other platforms get the bar buttons unless the environment asks for drawn traffic lights', () => {
  assert.equal(windowControls.resolveWindowControls('linux', {}, LINUX_RELEASE), windowControls.BUTTONS);
  assert.equal(
    windowControls.resolveWindowControls('linux', { MINDS_WINDOW_CONTROLS: '' }, LINUX_RELEASE),
    windowControls.BUTTONS
  );
  assert.equal(windowControls.resolveWindowControls('win32', {}, '10.0.22631'), windowControls.BUTTONS);
  assert.equal(
    windowControls.resolveWindowControls('linux', { MINDS_WINDOW_CONTROLS: 'mac' }, LINUX_RELEASE),
    windowControls.DRAWN_MAC
  );
});

test('under WSL the window keeps its native frame, unless the environment asks for drawn traffic lights', () => {
  assert.equal(windowControls.resolveWindowControls('linux', {}, WSL2_RELEASE), windowControls.NATIVE_FRAME);
  assert.equal(
    windowControls.resolveWindowControls('linux', { MINDS_WINDOW_CONTROLS: 'mac' }, WSL2_RELEASE),
    windowControls.DRAWN_MAC
  );
});

test('a value the variable does not accept is refused rather than read as a platform default', () => {
  assert.throws(
    () => windowControls.resolveWindowControls('linux', { MINDS_WINDOW_CONTROLS: 'osx' }, LINUX_RELEASE),
    /MINDS_WINDOW_CONTROLS="osx" is not a window style/
  );
});

test('the preload switch carries the resolved controls by name', () => {
  assert.equal(windowControls.windowControlsSwitch(windowControls.DRAWN_MAC), '--minds-window-controls=drawn-mac');
});
