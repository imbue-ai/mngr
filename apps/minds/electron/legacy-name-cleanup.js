'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

// CLEANUP: delete this module, its call in main.js, and legacy-name-cleanup.test.js
// once the Imbue Studio build has been on stable long enough for installs to
// have launched once (specs/imbue-studio-rename/05_cleanup.md).
//
// Kept free of any `electron` import (like session-persistence.js and
// update-channel.js) so it can be unit-tested under plain node.

// Every name the app has shipped under before this one, each of which owned
// its own set of per-user directories.
const LEGACY_APP_NAMES = Object.freeze(['Minds', 'Mind']);

function defaultEnvironment() {
  return { platform: process.platform, homeDir: os.homedir(), env: process.env };
}

/**
 * The per-user directories Electron gave the app under its previous names.
 *
 * The updater cache is deliberately absent: electron-builder derives its name
 * from the package name rather than the product name, so it did not move.
 */
function legacyNameDirs({ platform, homeDir, env } = defaultEnvironment()) {
  if (platform === 'darwin') {
    return LEGACY_APP_NAMES.flatMap((name) => [
      path.join(homeDir, 'Library', 'Application Support', name),
      path.join(homeDir, 'Library', 'Logs', name),
      path.join(homeDir, 'Library', 'Caches', name),
    ]);
  }
  const config = env.XDG_CONFIG_HOME || path.join(homeDir, '.config');
  const cache = env.XDG_CACHE_HOME || path.join(homeDir, '.cache');
  return LEGACY_APP_NAMES.flatMap((name) => [path.join(config, name), path.join(cache, name)]);
}

/**
 * Remove what the previous app name left behind, and return what was removed.
 *
 * Nothing is moved: `initSentry` opens the Crashpad database and the Sentry
 * queue at the new paths before this runs, so there is no empty destination to
 * move into. What is left behind is crash-reporter scratch.
 *
 * Repeating this is free once the directories are gone, so it needs no record
 * of having run, and a removal that fails is simply retried on the next launch.
 */
function removeLegacyNameDirs(environment = defaultEnvironment()) {
  const removed = [];
  for (const dir of legacyNameDirs(environment)) {
    if (!fs.existsSync(dir)) {
      continue;
    }
    try {
      fs.rmSync(dir, { recursive: true, force: true });
      removed.push(dir);
    } catch (err) {
      console.warn(`[legacy-name-cleanup] could not remove ${dir}: ${err.message}`);
    }
  }
  return removed;
}

module.exports = { removeLegacyNameDirs, legacyNameDirs, LEGACY_APP_NAMES };
