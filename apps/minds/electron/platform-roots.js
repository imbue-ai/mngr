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
// own default `userData` path from, and what the packagers name the macOS
// bundle and the Linux /opt directory. Mirrors MINDS_APP_NAME in
// imbue/minds/bootstrap.py.
const MINDS_APP_NAME = 'Imbue Studio';

// The directory the XDG base directories are keyed on. Keyed on `name` from
// package.json; mirrors XDG_APP_DIR_NAME in imbue/minds/bootstrap.py. The Apple
// roots take the display name instead, which no Linux data directory carries: a
// space there has to survive every shell word-splitting the path reaches.
const XDG_APP_DIR_NAME = 'imbue-studio';

const PRODUCTION_ROOT_NAME = 'minds';
const PRODUCTION_TIER = 'production';

// The two canonical layouts: Apple's `~/Library` roots, and the XDG base
// directories everywhere else.
const APPLE_PLATFORM = 'darwin';

// This app's own override: it points all three roots at one directory, and is set
// by the test fixtures, the CI steps and `minds-admin demo` for a demo box. Not a
// setting an end user is meant to export.
// It moves only these roots, never the legacy root the migration reads.
const STUDIO_ROOTS_OVERRIDE_ENV_VAR = 'STUDIO_ROOTS_OVERRIDE';

// CLEANUP: delete this and its check in platformRootsFor alongside
// _RETIRED_ROOTS_OVERRIDE_ENV_VAR in imbue/minds/bootstrap.py, once no shell,
// CI step or operator script still exports the old name.
// Ignoring it silently would hand a caller that still exports it the real roots
// while it believed it was isolated.
const RETIRED_ROOTS_OVERRIDE_ENV_VAR = 'MINDS_DATA_HOME';

// Each XDG base directory's default, relative to the home directory, used when
// the matching variable is unset, empty, or relative.
const XDG_DATA_HOME_SEGMENTS = ['.local', 'share'];
const XDG_CACHE_HOME_SEGMENTS = ['.cache'];
const XDG_STATE_HOME_SEGMENTS = ['.local', 'state'];

// The logs directory below the XDG state root. `log` rather than `logs` is the
// component systemd spells for a user unit's `LogsDirectory=`, which it places
// above the unit name rather than below it.
const XDG_LOG_ENTRY_NAME = 'log';

/**
 * One XDG base directory: the variable's path, else the default below `homeDir`.
 *
 * The spec reads an unset or empty variable as absent, and requires a relative
 * path to be ignored as invalid.
 */
function xdgBaseDir({ value, homeDir, defaultSegments }) {
  if (value && path.isAbsolute(value)) {
    return value;
  }
  return path.join(homeDir, ...defaultSegments);
}

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
 * `env` is the environment the roots are resolved against, passed rather than
 * read so one test can assert the macOS layout from Linux CI and the Linux
 * layout from a developer's Mac. It has no default, so a caller cannot pick up
 * an ambient value by omission.
 *
 * STUDIO_ROOTS_OVERRIDE outranks both layouts, collecting all three roles under
 * one throwaway directory. The three XDG base directories answer only off macOS,
 * one per role; macOS needs no counterpart because its roots are constants with
 * no variable to relocate them.
 *
 * Both real layouts give the tier a segment below a per-role parent, so one
 * tier's roots never nest inside another's.
 */
function platformRootsFor({ rootName, platform, homeDir, env }) {
  const tier = tierForRootName(rootName);
  if (env[RETIRED_ROOTS_OVERRIDE_ENV_VAR] && !env[STUDIO_ROOTS_OVERRIDE_ENV_VAR]) {
    throw new Error(
      `${RETIRED_ROOTS_OVERRIDE_ENV_VAR} is set but ${STUDIO_ROOTS_OVERRIDE_ENV_VAR} is not. The variable was ` +
        `renamed; export ${STUDIO_ROOTS_OVERRIDE_ENV_VAR} instead. Continuing would resolve the real roots while ` +
        'the caller expected a throwaway directory.'
    );
  }
  const rootsOverride = env[STUDIO_ROOTS_OVERRIDE_ENV_VAR];
  if (rootsOverride) {
    const tierRoot = path.join(rootsOverride, tier);
    return {
      state: path.join(tierRoot, 'state'),
      cache: path.join(tierRoot, 'cache'),
      logs: path.join(tierRoot, 'logs'),
    };
  }
  if (platform !== APPLE_PLATFORM) {
    // State holds credentials, agent records and the virtualenv, which is data
    // the user would miss, so it goes to XDG_DATA_HOME rather than to
    // XDG_CONFIG_HOME where Electron's own default `userData` would put it.
    return {
      state: path.join(xdgBaseDir({ value: env.XDG_DATA_HOME, homeDir, defaultSegments: XDG_DATA_HOME_SEGMENTS }), XDG_APP_DIR_NAME, tier),
      cache: path.join(xdgBaseDir({ value: env.XDG_CACHE_HOME, homeDir, defaultSegments: XDG_CACHE_HOME_SEGMENTS }), XDG_APP_DIR_NAME, tier),
      logs: path.join(
        xdgBaseDir({ value: env.XDG_STATE_HOME, homeDir, defaultSegments: XDG_STATE_HOME_SEGMENTS }),
        XDG_APP_DIR_NAME,
        tier,
        XDG_LOG_ENTRY_NAME
      ),
    };
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
 * The migration's source on every platform.
 */
function legacyDataDirFor({ rootName, homeDir }) {
  return path.join(homeDir, `.${rootName}`);
}

// Entries of the legacy root that are regenerable, and so belong under Caches
// where the OS may delete them. `.venv` and `.uv-python` are deliberately NOT
// here: they are regenerable too, but the backend cannot boot without them, so
// a low-disk purge would brick the app.
const CACHE_ENTRY_NAMES = ['.uv-cache', 'template-cache'];

// The legacy dotfolder entry whose *contents* become the logs root. Everything
// else keeps its own name one level down; this one is unwrapped because the
// logs root is already the log directory.
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
  XDG_APP_DIR_NAME,
  CACHE_ENTRY_NAMES,
  LOGS_ENTRY_NAME,
  XDG_DATA_HOME_SEGMENTS,
  XDG_CACHE_HOME_SEGMENTS,
  xdgBaseDir,
  MIGRATION_MARKER_NAME,
  tierForRootName,
  platformRootsFor,
  STUDIO_ROOTS_OVERRIDE_ENV_VAR,
  legacyDataDirFor,
  destinationForLegacyEntry,
  migrationPlanFor,
};
