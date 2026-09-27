// Unit tests for the display zoom preference.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const zoom = require('../../electron/display-zoom');

function tempDataDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'minds-zoom-'));
}

test('normalizeZoomPercent accepts only the offered percents', () => {
  for (const percent of zoom.ZOOM_PERCENTS) {
    assert.equal(zoom.normalizeZoomPercent(percent), percent);
  }
  assert.equal(zoom.normalizeZoomPercent(101), null);
  assert.equal(zoom.normalizeZoomPercent(125.5), null);
  assert.equal(zoom.normalizeZoomPercent('125'), null);
  assert.equal(zoom.normalizeZoomPercent(undefined), null);
  assert.equal(zoom.normalizeZoomPercent({ percent: 125 }), null);
});

test('a missing preference file reads as the default with no reason', () => {
  const dataDir = tempDataDir();
  assert.deepEqual(zoom.readZoomPercent(dataDir), { percent: zoom.DEFAULT_ZOOM_PERCENT, reason: null });
});

test('writeZoomPercent round-trips through readZoomPercent', () => {
  const dataDir = path.join(tempDataDir(), 'nested');
  zoom.writeZoomPercent(dataDir, 150);
  assert.deepEqual(zoom.readZoomPercent(dataDir), { percent: 150, reason: null });
});

test('a value the panel does not offer reads as the default with a reason', () => {
  const dataDir = tempDataDir();
  fs.writeFileSync(path.join(dataDir, zoom.PREFERENCE_FILENAME), JSON.stringify({ percent: 137 }));
  const read = zoom.readZoomPercent(dataDir);
  assert.equal(read.percent, zoom.DEFAULT_ZOOM_PERCENT);
  assert.match(read.reason, /unrecognized value 137/);
});

test('an unparseable file reads as the default with a reason', () => {
  const dataDir = tempDataDir();
  fs.writeFileSync(path.join(dataDir, zoom.PREFERENCE_FILENAME), '{not json');
  const read = zoom.readZoomPercent(dataDir);
  assert.equal(read.percent, zoom.DEFAULT_ZOOM_PERCENT);
  assert.match(read.reason, /unreadable/);
});

test('writeZoomPercent refuses a percent the panel does not offer', () => {
  const dataDir = tempDataDir();
  assert.throws(() => zoom.writeZoomPercent(dataDir, 137), /Refusing to store unknown display zoom 137/);
  assert.equal(fs.existsSync(path.join(dataDir, zoom.PREFERENCE_FILENAME)), false);
});

test('zoomFactorForPercent is the percent as a factor', () => {
  assert.equal(zoom.zoomFactorForPercent(100), 1);
  assert.equal(zoom.zoomFactorForPercent(125), 1.25);
  assert.equal(zoom.zoomFactorForPercent(80), 0.8);
});
