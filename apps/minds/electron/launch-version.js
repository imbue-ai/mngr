// Which app version the previous launch ran, so the renderer can tell the
// first launch into an app update (the auto-update prompt's first trigger).
//
// Free of any `electron` import, so it is testable under plain node: the
// caller passes the data directory and the version in.

const fs = require('fs');
const path = require('path');

const RECORD_FILENAME = 'last-run-version.json';

function recordPath(dataDir) {
  return path.join(dataDir, RECORD_FILENAME);
}

/** The version the record names, or null when there is no usable record. */
function readPreviousVersion(dataDir) {
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(recordPath(dataDir), 'utf8'));
  } catch (err) {
    if (err.code !== 'ENOENT') console.warn(`[launch-version] ignoring an unreadable record: ${err.message}`);
    return null;
  }
  return raw && typeof raw.version === 'string' && raw.version !== '' ? raw.version : null;
}

/**
 * Compare this launch's version with the recorded one, then record this one.
 *
 * With no record, `hasRunBefore` decides: an install that has run before (it
 * left other state behind) is launching a build that predates the record,
 * which is itself an update; a fresh install is not. A failed write is logged
 * and the comparison still returned, at worst the next launch asks again.
 */
function recordLaunchVersion(dataDir, currentVersion, { hasRunBefore }) {
  const previousVersion = readPreviousVersion(dataDir);
  const isFirstLaunchAfterUpdate = previousVersion === null ? hasRunBefore : previousVersion !== currentVersion;
  try {
    fs.mkdirSync(dataDir, { recursive: true });
    fs.writeFileSync(recordPath(dataDir), JSON.stringify({ version: currentVersion }, null, 2) + '\n');
  } catch (err) {
    console.warn(`[launch-version] could not record this launch's version: ${err.message}`);
  }
  return { previousVersion, isFirstLaunchAfterUpdate };
}

module.exports = { RECORD_FILENAME, recordLaunchVersion };
