'use strict';

// The platform-canonical directories a minds tier stores state under. Kept free
// of any `electron` import so it can be unit-tested under plain node (see
// ../test/unit/platform-roots.test.js). paths.js delegates to it.
//
// Pure counterpart to imbue.minds.bootstrap. The shell owns resolution because
// it creates the virtualenv before any Python exists to ask; the backend honors
// MINDS_STATE_DIR / MINDS_CACHE_DIR / MINDS_LOG_DIR ahead of its own resolver,
// so once the shell exports them the two runtimes cannot disagree about the
// layout (see specs/minds-platform-canonical-dirs/spec.md, F7).
const path = require('path');

// Keyed on `productName` from package.json, which is what Electron derives its
// own default `userData` path from. Mirrors MINDS_APP_NAME in
// imbue/minds/bootstrap.py.
const MINDS_APP_NAME = 'Imbue Studio';

const PRODUCTION_ROOT_NAME = 'minds';
const PRODUCTION_TIER = 'production';

// The one platform with its own canonical roots; everywhere else keeps the single dotfolder.
const APPLE_PLATFORM = 'darwin';

/**
 * `minds` -> `production`; `minds-<name>` -> `<name>`.
 *
 * Mirrors imbue.minds.bootstrap.env_name_from_root_name. `minds-staging` and
 * `minds-dev-staging` are different tiers and must not collapse into one.
 */
function tierForRootName(rootName) {
  if (rootName === PRODUCTION_ROOT_NAME) {
    return PRODUCTION_TIER;
  }
  const prefix = `${PRODUCTION_ROOT_NAME}-`;
  if (!rootName.startsWith(prefix)) {
    throw new Error(`Cannot extract a tier from MINDS_ROOT_NAME=${JSON.stringify(rootName)}: expected 'minds' or 'minds-<env-name>'.`);
  }
  return rootName.slice(prefix.length);
}

/**
 * The `{ state, cache, logs }` roots for one tier.
 *
 * `dataHome` (MINDS_DATA_HOME) collects all three under one throwaway
 * directory, which is how tests and the CI runner stay self-contained.
 * Only macOS has canonical roots; every other platform keeps the single
 * dotfolder, whose name already carries the tier.
 */
function platformRootsFor({ rootName, platform, homeDir, dataHome }) {
  const tier = tierForRootName(rootName);
  if (dataHome) {
    const tierRoot = path.join(dataHome, tier);
    return {
      state: path.join(tierRoot, 'state'),
      cache: path.join(tierRoot, 'cache'),
      logs: path.join(tierRoot, 'logs'),
    };
  }
  if (platform !== APPLE_PLATFORM) {
    const dotfolder = legacyDataDirFor({ rootName, homeDir });
    return { state: dotfolder, cache: dotfolder, logs: path.join(dotfolder, LOGS_ENTRY_NAME) };
  }
  return {
    state: path.join(homeDir, 'Library', 'Application Support', MINDS_APP_NAME, tier),
    cache: path.join(homeDir, 'Library', 'Caches', MINDS_APP_NAME, tier),
    logs: path.join(homeDir, 'Library', 'Logs', MINDS_APP_NAME, tier),
  };
}

/**
 * The `~/.<rootName>` root this tier used before the move.
 *
 * The migration's source on macOS; off macOS still the live root that
 * `platformRootsFor` resolves every role into.
 */
function legacyDataDirFor({ rootName, homeDir }) {
  return path.join(homeDir, `.${rootName}`);
}

// Entries of the legacy root that are regenerable, and so belong under Caches
// where the OS may delete them. `.venv` and `.uv-python` are deliberately NOT
// here: they are regenerable too, but the backend cannot boot without them, so
// a low-disk purge would brick the app.
const CACHE_ENTRY_NAMES = ['.uv-cache', 'template-cache'];

// The legacy entry whose *contents* become the logs root. Everything else keeps
// its own name one level down; this one is unwrapped because the logs root is
// already the log directory.
const LOGS_ENTRY_NAME = 'logs';

// Written into the state root once the move has completed. Its presence is what
// makes the migration idempotent, so it is written last and only on success.
const MIGRATION_MARKER_NAME = '.migrated-from-dotfolder';

/**
 * Where one entry of the legacy root belongs, as `{ role, relativePath }`.
 *
 * Leaf names are preserved so the move is a pure rename and the result can be
 * diffed against the original.
 */
function destinationForLegacyEntry(entryName) {
  if (entryName === LOGS_ENTRY_NAME) {
    return { role: 'logs', relativePath: '' };
  }
  if (CACHE_ENTRY_NAMES.includes(entryName)) {
    return { role: 'cache', relativePath: entryName };
  }
  return { role: 'state', relativePath: entryName };
}

/**
 * The full move list for a legacy root, as `[{ from, to }]`.
 *
 * `entryNames` is the direct children of the legacy directory. The `logs` entry
 * expands to one move per child so its contents land at the logs root rather
 * than in a `logs/` subdirectory of it; every other entry is a single move.
 * `logsEntryChildNames` supplies those children (empty when there is no `logs`).
 */
function migrationPlanFor({ legacyDir, roots, entryNames, logsEntryChildNames = [] }) {
  const moves = [];
  for (const entryName of entryNames) {
    if (entryName === MIGRATION_MARKER_NAME) {
      continue;
    }
    const { role, relativePath } = destinationForLegacyEntry(entryName);
    if (role === 'logs') {
      for (const childName of logsEntryChildNames) {
        moves.push({
          from: path.join(legacyDir, entryName, childName),
          to: path.join(roots.logs, childName),
        });
      }
      continue;
    }
    moves.push({ from: path.join(legacyDir, entryName), to: path.join(roots[role], relativePath) });
  }
  return moves;
}

module.exports = {
  MINDS_APP_NAME,
  CACHE_ENTRY_NAMES,
  LOGS_ENTRY_NAME,
  MIGRATION_MARKER_NAME,
  tierForRootName,
  platformRootsFor,
  legacyDataDirFor,
  destinationForLegacyEntry,
  migrationPlanFor,
};
