// The bar's height is written down three times: the SPA's stylesheet draws the
// bar at it, main places the macOS traffic lights and the pull-out surface by
// it, and the loading document parks its mark in a band of it. Nothing at
// runtime connects the three, so this pins them to one another.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const { TITLEBAR_HEIGHT } = require('../../electron/popout-policy');

const SPA_STYLESHEET = path.join(__dirname, '..', '..', 'frontend', 'src', 'style.css');
const LOADING_DOCUMENT = path.join(__dirname, '..', '..', 'electron', 'shell.html');

// The one `--titlebar-height: <N>px;` declaration in a file, as N.
function declaredTitlebarHeight(file) {
  const declarations = [...fs.readFileSync(file, 'utf8').matchAll(/--titlebar-height:\s*(\d+)px\s*;/g)];
  assert.equal(declarations.length, 1, `${file} should declare --titlebar-height in px exactly once`);
  return Number(declarations[0][1]);
}

test("the SPA's bar height is Electron's TITLEBAR_HEIGHT", () => {
  assert.equal(declaredTitlebarHeight(SPA_STYLESHEET), TITLEBAR_HEIGHT);
});

test("the loading document's bar height is Electron's TITLEBAR_HEIGHT", () => {
  assert.equal(declaredTitlebarHeight(LOADING_DOCUMENT), TITLEBAR_HEIGHT);
});

test('the bar is 44px tall', () => {
  assert.equal(TITLEBAR_HEIGHT, 44);
});
