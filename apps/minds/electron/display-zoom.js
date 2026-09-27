// Display zoom preference: the one scale every window renders at.
//
// Deliberately free of any `electron` import (paths.js pulls in `app`) so the
// rules are unit-testable under plain node -- the caller passes the data
// directory in.

const fs = require('fs');
const path = require('path');

// The sizes Settings > Display offers. A stored value outside this list reads
// as the default rather than being clamped, so a hand-edited file can never
// produce a size the panel cannot show as selected.
const ZOOM_PERCENTS = [80, 90, 100, 110, 125, 150, 175, 200];
const DEFAULT_ZOOM_PERCENT = 100;

const PREFERENCE_FILENAME = 'display-zoom.json';

function preferencePath(dataDir) {
  return path.join(dataDir, PREFERENCE_FILENAME);
}

/** Resolve a raw value to an offered percent, or null for anything else. */
function normalizeZoomPercent(raw) {
  return Number.isInteger(raw) && ZOOM_PERCENTS.includes(raw) ? raw : null;
}

/**
 * Read the stored percent, falling back to the default when the file is
 * missing, unreadable, or carries a value the panel does not offer.
 *
 * `reason` says why the fallback was used, or is null on a clean read (and on
 * a missing file, which is every install before the setting is first changed).
 */
function readZoomPercent(dataDir) {
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(preferencePath(dataDir), 'utf8'));
  } catch (err) {
    const reason = err.code === 'ENOENT' ? null : `unreadable (${err.message})`;
    return { percent: DEFAULT_ZOOM_PERCENT, reason };
  }
  const percent = normalizeZoomPercent(raw && raw.percent);
  if (percent === null) {
    return { percent: DEFAULT_ZOOM_PERCENT, reason: `unrecognized value ${JSON.stringify(raw && raw.percent)}` };
  }
  return { percent, reason: null };
}

function writeZoomPercent(dataDir, percent) {
  if (normalizeZoomPercent(percent) === null) {
    throw new Error(`Refusing to store unknown display zoom ${JSON.stringify(percent)}`);
  }
  fs.mkdirSync(dataDir, { recursive: true });
  fs.writeFileSync(preferencePath(dataDir), JSON.stringify({ percent }, null, 2) + '\n');
}

function zoomFactorForPercent(percent) {
  return percent / 100;
}

module.exports = {
  ZOOM_PERCENTS,
  DEFAULT_ZOOM_PERCENT,
  PREFERENCE_FILENAME,
  normalizeZoomPercent,
  readZoomPercent,
  writeZoomPercent,
  zoomFactorForPercent,
};
