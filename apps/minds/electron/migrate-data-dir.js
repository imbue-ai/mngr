'use strict';

// One-shot move of a tier's files from the legacy `~/.<MINDS_ROOT_NAME>` root
// onto the platform-canonical state / cache / logs roots.
//
// CLEANUP: delete this module, its call in main.js, and
// test/unit/migrate-data-dir.test.js once no install can still be holding an
// unmigrated ~/.minds (specs/minds-platform-canonical-dirs/spec.md, "Retiring the migration").
//
// Runs in the shell rather than the backend because the shell creates the
// virtualenv (env-setup.js) and then spawns Python into it: by the time any
// Python exists to run a migration, it is already running from a path this
// migration would have moved.
const fs = require('fs');
const path = require('path');

const { MIGRATION_MARKER_NAME, migrationPlanFor } = require('./platform-roots');

// The state root's subtrees whose files point at the legacy root rather than
// merely mentioning it, with the predicate that picks the ones to rewrite.
// Deliberately narrow: agent transcripts under `mngr/preserved/` and every log
// under `mngr/events/`, `logs/` and `latchkey/` also contain the old path, but
// as recorded content that must be left exactly as written.
const RECORDED_PATH_SCOPES = [
  // mngr records the absolute path of each host's SSH key inside its
  // per-provider host state, so moving `mngr/` invalidates those keys unless
  // the recorded prefix moves with them.
  { relativeDir: path.join('mngr', 'profiles'), isRewritable: (fileName) => fileName.endsWith('.json') },
];

// Generated files that point at the old root and must not be carried across by
// rewriting. mngr bakes the interpreter's absolute path into its completion
// function bodies, and older ones baked it in unquoted -- rewriting the prefix
// to a state root containing a space ("Application Support") yields a file that
// word-splits at the first space and errors on every tab press. mngr already
// regenerates these on demand, and the rc snippet that sources them is guarded
// by `[ -r ... ]`, so removing them degrades to "no completions until mngr
// reinstalls them" rather than to a broken shell.
const STALE_GENERATED_DIRS = [path.join('mngr', 'completions')];

// Where a failed migration leaves its account of itself. The log root is only
// ever a destination of the move, never a source, so writing here cannot
// disturb the retry on the next launch.
const MIGRATION_FAILURE_LOG_NAME = 'migration-failure.log';

/**
 * Whether `candidatePath` is a directory.
 *
 * Only an absent path answers false. Any other stat failure propagates, because
 * the one asked about first is the legacy root: swallowing an unreadable one
 * would take the no-legacy-dir path, write the marker, and orphan the user's
 * data behind a migration recorded as complete.
 */
function isDirectory(candidatePath) {
  try {
    return fs.statSync(candidatePath).isDirectory();
  } catch (err) {
    if (err.code === 'ENOENT') {
      return false;
    }
    throw err;
  }
}

/**
 * Move one path, falling back to copy+delete across filesystems.
 *
 * `fs.renameSync` fails with EXDEV when the legacy root and the destination are
 * on different volumes, which happens on a home directory mounted separately
 * from `~/Library`.
 */
function movePath(from, to) {
  fs.mkdirSync(path.dirname(to), { recursive: true });
  try {
    fs.renameSync(from, to);
  } catch (err) {
    if (err.code !== 'EXDEV') {
      throw err;
    }
    fs.cpSync(from, to, { recursive: true });
    fs.rmSync(from, { recursive: true, force: true });
  }
}

/**
 * Rewrite the legacy root's absolute path to the new one everywhere
 * RECORDED_PATH_SCOPES says it is a pointer, so host SSH keys and the
 * completion shims still resolve after the move.
 */
function rewriteRecordedPaths({ stateDir, legacyDir, log }) {
  let rewrittenCount = 0;
  for (const { relativeDir, isRewritable } of RECORDED_PATH_SCOPES) {
    const scopeDir = path.join(stateDir, relativeDir);
    if (!isDirectory(scopeDir)) {
      continue;
    }
    const stack = [scopeDir];
    while (stack.length > 0) {
      const currentDir = stack.pop();
      for (const entry of fs.readdirSync(currentDir, { withFileTypes: true })) {
        const entryPath = path.join(currentDir, entry.name);
        if (entry.isDirectory()) {
          stack.push(entryPath);
          continue;
        }
        if (!entry.isFile() || !isRewritable(entry.name)) {
          continue;
        }
        const before = fs.readFileSync(entryPath, 'utf8');
        if (!before.includes(legacyDir)) {
          continue;
        }
        fs.writeFileSync(entryPath, before.split(legacyDir).join(stateDir));
        rewrittenCount += 1;
        log(`[migrate] rewrote recorded paths in ${entryPath}`);
      }
    }
  }
  return rewrittenCount;
}

/**
 * Move a tier's files onto its canonical roots, once.
 *
 * Returns `{ migrated, reason }`. Idempotent: a marker in the state root,
 * written only after every move and the path rewrite have succeeded, means a
 * crash partway through re-runs cleanly rather than being recorded as done.
 * The legacy directory is left in place (emptied) rather than deleted, so a
 * user who wants their old state back can still find where it was.
 */
function migrateLegacyDataDir({ legacyDir, roots, log = console.log }) {
  if (path.resolve(legacyDir) === path.resolve(roots.state)) {
    // The layouts coincide off macOS, so there is nowhere to move to and no
    // marker to leave in a directory the user still keeps.
    return { migrated: false, reason: 'state-root-is-legacy-root' };
  }
  const markerPath = path.join(roots.state, MIGRATION_MARKER_NAME);
  if (fs.existsSync(markerPath)) {
    return { migrated: false, reason: 'already-migrated' };
  }
  if (!isDirectory(legacyDir)) {
    // A fresh install has no legacy root. Mark it so later launches skip the
    // existence check entirely, and so the state is indistinguishable from a
    // completed migration.
    fs.mkdirSync(roots.state, { recursive: true });
    fs.writeFileSync(markerPath, `no legacy directory at ${legacyDir}\n`);
    return { migrated: false, reason: 'no-legacy-dir' };
  }

  const entryNames = fs.readdirSync(legacyDir);
  const logsDir = path.join(legacyDir, 'logs');
  const logsEntryChildNames = isDirectory(logsDir) ? fs.readdirSync(logsDir) : [];
  const moves = migrationPlanFor({ legacyDir, roots, entryNames, logsEntryChildNames });

  log(`[migrate] moving ${moves.length} entries out of ${legacyDir}`);
  let movedCount = 0;
  for (const { from, to } of moves) {
    if (!fs.existsSync(from)) {
      continue;
    }
    if (fs.existsSync(to)) {
      // The destination already holds this entry -- Sentry and Crashpad write
      // into the state root before this migration can run on a first launch.
      // Keeping the destination is right: it is the newer of the two.
      log(`[migrate] skipping ${from}: ${to} already exists`);
      continue;
    }
    movePath(from, to);
    movedCount += 1;
  }

  const rewrittenCount = rewriteRecordedPaths({ stateDir: roots.state, legacyDir, log });

  for (const relativeDir of STALE_GENERATED_DIRS) {
    const staleDir = path.join(roots.state, relativeDir);
    if (isDirectory(staleDir)) {
      fs.rmSync(staleDir, { recursive: true, force: true });
      log(`[migrate] removed ${staleDir}; mngr regenerates it against the new root`);
    }
  }

  fs.mkdirSync(roots.state, { recursive: true });
  fs.writeFileSync(
    markerPath,
    `migrated ${movedCount} entries from ${legacyDir}\nrewrote ${rewrittenCount} recorded-path files\n`
  );
  log(`[migrate] moved ${movedCount} entries; left ${legacyDir} in place`);
  return { migrated: true, reason: 'migrated', movedCount, rewrittenCount };
}

/**
 * Record why the migration failed, and return the text recorded.
 *
 * The caller runs before the log tee and Sentry are up (both open files under
 * the roots being moved), so without this a failed migration ends the launch
 * with nothing written anywhere.
 */
function recordMigrationFailure({ roots, legacyDir, error, log = console.error }) {
  const detail =
    `Minds could not finish moving its data out of ${legacyDir}.\n\n` +
    `${error && error.stack ? error.stack : error}\n\n` +
    'Nothing was deleted; whatever has not moved yet is still there, and the next launch retries.';
  try {
    fs.mkdirSync(roots.logs, { recursive: true });
    fs.appendFileSync(path.join(roots.logs, MIGRATION_FAILURE_LOG_NAME), `${new Date().toISOString()}\n${detail}\n\n`);
  } catch (writeError) {
    log(`[migrate] could not record the failure under ${roots.logs}: ${writeError && writeError.message}`);
  }
  return detail;
}

module.exports = {
  MIGRATION_FAILURE_LOG_NAME,
  migrateLegacyDataDir,
  movePath,
  recordMigrationFailure,
  rewriteRecordedPaths,
};
