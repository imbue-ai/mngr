'use strict';
//
// CLEANUP: delete alongside electron/migrate-data-dir.js (specs/minds-platform-canonical-dirs/spec.md, "Retiring the migration").

// Unit tests for electron/migrate-data-dir.js (the one-shot move off
// ~/.<MINDS_ROOT_NAME>) and the pure plan in electron/platform-roots.js.
//
// Run with: pnpm test:unit  (node --test test/unit/*.test.js)
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { migrationPlanFor, destinationForLegacyEntry, MIGRATION_MARKER_NAME } = require('../../electron/platform-roots');
const {
  MIGRATION_FAILURE_LOG_NAME,
  migrateLegacyDataDir,
  recordMigrationFailure,
} = require('../../electron/migrate-data-dir');

function makeScratch(t) {
  // The space is deliberate: the real state root is under "Application
  // Support", and a path with a space is what breaks generated shell and
  // whitespace-splitting cmdline parsers. Tests on a space-free tmp dir
  // cannot see that class of bug at all.
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'minds migrate '));
  t.after(() => fs.rmSync(base, { recursive: true, force: true }));
  const legacyDir = path.join(base, '.minds');
  const roots = {
    state: path.join(base, 'state'),
    cache: path.join(base, 'cache'),
    logs: path.join(base, 'logs'),
  };
  return { base, legacyDir, roots };
}

function writeFileAt(filePath, contents) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, contents);
}

const quiet = () => {};

// -- destinationForLegacyEntry --

test('regenerable entries go to cache and everything else to state', () => {
  assert.deepEqual(destinationForLegacyEntry('.uv-cache'), { role: 'cache', relativePath: '.uv-cache' });
  assert.deepEqual(destinationForLegacyEntry('template-cache'), { role: 'cache', relativePath: 'template-cache' });
  assert.deepEqual(destinationForLegacyEntry('mngr'), { role: 'state', relativePath: 'mngr' });
  assert.deepEqual(destinationForLegacyEntry('auth'), { role: 'state', relativePath: 'auth' });
});

test('the venv and the interpreter are state, not cache', () => {
  // Regenerable, but the backend cannot boot without them, so they must not sit
  // anywhere the OS may reclaim.
  assert.equal(destinationForLegacyEntry('.venv').role, 'state');
  assert.equal(destinationForLegacyEntry('.uv-python').role, 'state');
});

test('the logs entry is unwrapped rather than nested', () => {
  assert.deepEqual(destinationForLegacyEntry('logs'), { role: 'logs', relativePath: '' });
});

// -- migrationPlanFor --

test('the plan unwraps logs children and keeps every other leaf name', () => {
  const moves = migrationPlanFor({
    legacyDir: '/legacy',
    roots: { state: '/state', cache: '/cache', logs: '/logs' },
    entryNames: ['mngr', '.uv-cache', 'logs'],
    logsEntryChildNames: ['minds.log', 'minds-events.jsonl'],
  });
  assert.deepEqual(moves, [
    { from: '/legacy/mngr', to: '/state/mngr' },
    { from: '/legacy/.uv-cache', to: '/cache/.uv-cache' },
    { from: '/legacy/logs/minds.log', to: '/logs/minds.log' },
    { from: '/legacy/logs/minds-events.jsonl', to: '/logs/minds-events.jsonl' },
  ]);
});

test('the plan never tries to move the marker onto itself', () => {
  const moves = migrationPlanFor({
    legacyDir: '/legacy',
    roots: { state: '/state', cache: '/cache', logs: '/logs' },
    entryNames: [MIGRATION_MARKER_NAME, 'auth'],
  });
  assert.deepEqual(moves, [{ from: '/legacy/auth', to: '/state/auth' }]);
});

// -- migrateLegacyDataDir --

test('a populated legacy root is split across the three roots', (t) => {
  const { legacyDir, roots } = makeScratch(t);
  writeFileAt(path.join(legacyDir, 'auth', 'sessions', 'a.json'), '{"token":"secret"}');
  writeFileAt(path.join(legacyDir, 'mngr', 'config.toml'), 'profile = "p"\n');
  writeFileAt(path.join(legacyDir, '.uv-cache', 'blob'), 'cached');
  writeFileAt(path.join(legacyDir, 'template-cache', 'repo', 'HEAD'), 'ref');
  writeFileAt(path.join(legacyDir, '.venv', 'pyvenv.cfg'), 'home = /x');
  writeFileAt(path.join(legacyDir, 'logs', 'minds.log'), 'hello');

  const result = migrateLegacyDataDir({ legacyDir, roots, log: quiet });
  assert.equal(result.migrated, true);

  assert.equal(fs.readFileSync(path.join(roots.state, 'auth', 'sessions', 'a.json'), 'utf8'), '{"token":"secret"}');
  assert.equal(fs.readFileSync(path.join(roots.state, 'mngr', 'config.toml'), 'utf8'), 'profile = "p"\n');
  assert.equal(fs.readFileSync(path.join(roots.state, '.venv', 'pyvenv.cfg'), 'utf8'), 'home = /x');
  assert.equal(fs.readFileSync(path.join(roots.cache, '.uv-cache', 'blob'), 'utf8'), 'cached');
  assert.equal(fs.readFileSync(path.join(roots.cache, 'template-cache', 'repo', 'HEAD'), 'utf8'), 'ref');
  // Unwrapped: the log lands at the logs root, not logs/logs/.
  assert.equal(fs.readFileSync(path.join(roots.logs, 'minds.log'), 'utf8'), 'hello');
  assert.equal(fs.existsSync(path.join(roots.logs, 'logs')), false);

  // Secrets did not follow the cache onto a root the OS may reclaim.
  assert.equal(fs.existsSync(path.join(roots.cache, 'auth')), false);
});

test('mngr records the moved key path, not the one it was moved from', (t) => {
  // mngr stores each host's SSH key as an absolute path; leaving the old one
  // behind would break SSH to every existing agent after the move.
  const { legacyDir, roots } = makeScratch(t);
  const hostState = path.join(legacyDir, 'mngr', 'profiles', 'p1', 'providers', 'lima', 'state', 'host_state', 'host-1.json');
  writeFileAt(hostState, JSON.stringify({ ssh_key: path.join(legacyDir, 'mngr', 'profiles', 'p1', 'keys', 'root_ssh_key') }));

  const result = migrateLegacyDataDir({ legacyDir, roots, log: quiet });
  assert.equal(result.rewrittenCount, 1);

  const moved = path.join(roots.state, 'mngr', 'profiles', 'p1', 'providers', 'lima', 'state', 'host_state', 'host-1.json');
  const recorded = JSON.parse(fs.readFileSync(moved, 'utf8')).ssh_key;
  assert.equal(recorded, path.join(roots.state, 'mngr', 'profiles', 'p1', 'keys', 'root_ssh_key'));
  assert.ok(!recorded.includes(legacyDir));
});

test('the generated completion shims are dropped, not rewritten', (t) => {
  // mngr.zsh / mngr.bash invoke <root>/.venv/bin/python3 by absolute path, and
  // older ones bake it in unquoted -- so rewriting the prefix to a state root
  // containing a space produces a file that word-splits at "Application
  // Support" and errors on every tab press. mngr regenerates these on demand
  // and the rc snippet sourcing them is `[ -r ... ]`-guarded, so dropping them
  // degrades to "no completions until mngr reinstalls" rather than to a broken
  // shell.
  const { legacyDir, roots } = makeScratch(t);
  const shim = path.join(legacyDir, 'mngr', 'completions', 'mngr.zsh');
  writeFileAt(shim, `completions=$(${path.join(legacyDir, '.venv', 'bin', 'python3')} -m imbue.mngr.cli.complete)\n`);

  migrateLegacyDataDir({ legacyDir, roots, log: quiet });

  assert.equal(fs.existsSync(path.join(roots.state, 'mngr', 'completions')), false);
  // The rest of the mngr tree still came across.
  assert.equal(fs.existsSync(path.join(roots.state, 'mngr')), true);
});

test('agent transcripts keep the old path as message content', (t) => {
  // preserved/ holds recorded agent output. The old path appears there as text
  // the user wrote, not as a pointer to rewrite.
  const { legacyDir, roots } = makeScratch(t);
  const transcript = path.join(legacyDir, 'mngr', 'preserved', 'agent-1', 'events.jsonl');
  const line = JSON.stringify({ text: `I looked in ${legacyDir}/mngr` });
  writeFileAt(transcript, line);

  migrateLegacyDataDir({ legacyDir, roots, log: quiet });

  const moved = path.join(roots.state, 'mngr', 'preserved', 'agent-1', 'events.jsonl');
  assert.equal(fs.readFileSync(moved, 'utf8'), line);
});

test('running twice moves nothing the second time', (t) => {
  const { legacyDir, roots } = makeScratch(t);
  writeFileAt(path.join(legacyDir, 'auth', 'a.json'), 'first');

  assert.equal(migrateLegacyDataDir({ legacyDir, roots, log: quiet }).migrated, true);

  // A file that reappears at the legacy root afterwards is not swept up: the
  // migration is over, and the legacy root is no longer an input.
  writeFileAt(path.join(legacyDir, 'auth-late', 'b.json'), 'late');
  const second = migrateLegacyDataDir({ legacyDir, roots, log: quiet });
  assert.equal(second.migrated, false);
  assert.equal(second.reason, 'already-migrated');
  assert.equal(fs.existsSync(path.join(roots.state, 'auth-late')), false);
});

test('an entry already at the destination is kept, not overwritten', (t) => {
  // Sentry and Crashpad write into the state root before the migration can run
  // on a first launch, so the destination copy is the newer one.
  const { legacyDir, roots } = makeScratch(t);
  writeFileAt(path.join(legacyDir, 'sentry', 'scope.json'), 'old');
  writeFileAt(path.join(roots.state, 'sentry', 'scope.json'), 'new');

  migrateLegacyDataDir({ legacyDir, roots, log: quiet });

  assert.equal(fs.readFileSync(path.join(roots.state, 'sentry', 'scope.json'), 'utf8'), 'new');
});

test('a legacy root that cannot be read is not recorded as nothing to migrate', (t) => {
  // Only an absent legacy root means "fresh install". Treating any other stat
  // failure that way would write the marker and orphan the user's data behind a
  // migration recorded as complete.
  const { base, roots } = makeScratch(t);
  const blocker = path.join(base, 'blocker');
  writeFileAt(blocker, 'not a directory');
  const legacyDir = path.join(blocker, '.minds');

  assert.throws(() => migrateLegacyDataDir({ legacyDir, roots, log: quiet }), { code: 'ENOTDIR' });
  assert.equal(fs.existsSync(path.join(roots.state, MIGRATION_MARKER_NAME)), false);
});

test('a fresh install is marked without inventing a legacy directory', (t) => {
  const { legacyDir, roots } = makeScratch(t);
  const result = migrateLegacyDataDir({ legacyDir, roots, log: quiet });
  assert.equal(result.migrated, false);
  assert.equal(result.reason, 'no-legacy-dir');
  assert.equal(fs.existsSync(path.join(roots.state, MIGRATION_MARKER_NAME)), true);
  assert.equal(fs.existsSync(legacyDir), false);
});

// -- recordMigrationFailure --

test('a failed migration leaves an account of itself under the log root', (t) => {
  // It runs before the log tee and Sentry, so this file is the only trace a
  // failed launch leaves behind.
  const { legacyDir, roots } = makeScratch(t);
  const detail = recordMigrationFailure({ roots, legacyDir, error: new Error('EACCES: denied'), log: quiet });

  const recorded = fs.readFileSync(path.join(roots.logs, MIGRATION_FAILURE_LOG_NAME), 'utf8');
  assert.ok(recorded.includes('EACCES: denied'));
  assert.ok(recorded.includes(legacyDir));
  assert.ok(detail.includes('EACCES: denied'));
});

test('a log root that cannot be written does not replace the original failure', (t) => {
  const { base, legacyDir, roots } = makeScratch(t);
  // A file where the log root should be: mkdir fails, and the caller still has
  // the text to show and the original error to rethrow.
  writeFileAt(path.join(base, 'logs'), 'not a directory');
  const detail = recordMigrationFailure({ roots, legacyDir, error: new Error('ENOSPC'), log: quiet });
  assert.ok(detail.includes('ENOSPC'));
});

test('every destination path really does contain a space', (t) => {
  // Guards the guard: if makeScratch ever loses its space, the suite silently
  // stops covering the failure mode it was widened to cover.
  const { roots } = makeScratch(t);
  for (const root of Object.values(roots)) {
    assert.ok(root.includes(' '), `${root} has no space; the fixture no longer models the real layout`);
  }
});

test('the legacy directory survives the move', (t) => {
  // Deleting it outright would give a user no way back to state they expected
  // to find in their home directory.
  const { legacyDir, roots } = makeScratch(t);
  writeFileAt(path.join(legacyDir, 'auth', 'a.json'), 'x');
  migrateLegacyDataDir({ legacyDir, roots, log: quiet });
  assert.equal(fs.existsSync(legacyDir), true);
});
