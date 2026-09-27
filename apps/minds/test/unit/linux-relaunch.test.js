// Unit tests for the Linux relaunch-after-exit spawner.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { relaunchAfterExitCommand, startRelaunchAfterExit } = require('../../electron/linux-relaunch');

// A stand-in for the exiting app: a process that lives a little longer than
// the relaunch command takes to start. It is an orphaned grandchild rather
// than a child of the test, because a child that exits while spawnSync
// blocks the event loop stays a zombie, and `kill -0` still succeeds on a
// zombie. Its stdio must not be spawnSync's pipes either: spawnSync returns
// only once every writer of those pipes has closed, which would make it sit
// through the stand-in's whole lifetime and hand over an already-dead pid.
function startOutgoingStandIn() {
  const pid = spawnSync('/bin/sh', ['-c', 'sleep 0.4 </dev/null >/dev/null 2>&1 & echo $!'], {
    encoding: 'utf8',
  }).stdout.trim();
  assert.match(pid, /^\d+$/);
  return Number(pid);
}

// The "executable" a relaunch runs in these tests: it reports, at the moment
// it runs, whether the outgoing process is still there, then echoes the
// arguments it was handed.
const REPORT_SCRIPT = 'if kill -0 "$1" 2>/dev/null; then echo alive; else echo gone; fi; shift; echo "$@"';

test('the relaunch command starts the executable with its arguments only once the old process is gone', () => {
  // The command must outlive the stand-in and then exec the executable (here
  // /bin/sh running the report script) with the arguments intact.
  const outgoingPid = startOutgoingStandIn();
  const { command, args } = relaunchAfterExitCommand({
    pid: outgoingPid,
    executablePath: '/bin/sh',
    args: ['-c', REPORT_SCRIPT, 'report', String(outgoingPid), 'minds://create?git_url=x', '--some-switch'],
  });
  const result = spawnSync(command, args, { encoding: 'utf8', timeout: 10000 });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, 'gone\nminds://create?git_url=x --some-switch\n');
});

test('the relaunch command names the shell, the pid, and the executable positionally', () => {
  const { command, args } = relaunchAfterExitCommand({ pid: 4242, executablePath: '/opt/Mind/minds', args: ['a', 'b'] });
  assert.equal(command, '/bin/sh');
  assert.deepEqual(args.slice(-4), ['4242', '/opt/Mind/minds', 'a', 'b']);
  assert.equal(args[0], '-c');
});

test('the spawner starts the executable detached, with its arguments, after the old process is gone', async (t) => {
  // Here the spawner itself runs, so the report has to come back through a
  // file rather than a pipe.
  const outgoingPid = startOutgoingStandIn();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'minds-relaunch-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const reportPath = path.join(dir, 'report');
  // Written under another name and renamed into place: the shell creates a
  // redirect's target before running the commands that fill it, and the poll
  // below reads the file as soon as it exists.
  const reportScript = `{ ${REPORT_SCRIPT}; } > "$0.partial" && mv "$0.partial" "$0"`;
  const replacement = startRelaunchAfterExit({
    pid: outgoingPid,
    executablePath: '/bin/sh',
    args: ['-c', reportScript, reportPath, String(outgoingPid), 'minds://create?git_url=x', '--some-switch'],
  });
  await new Promise((resolve, reject) => {
    replacement.once('spawn', resolve);
    replacement.once('error', reject);
  });
  const deadline = Date.now() + 10000;
  while (!fs.existsSync(reportPath) && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(fs.readFileSync(reportPath, 'utf8'), 'gone\nminds://create?git_url=x --some-switch\n');
});
