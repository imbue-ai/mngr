// Unit tests for the Linux sandbox relaunch decision.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');

const {
  NO_SANDBOX_SWITCH,
  RELAUNCHED_SWITCH,
  sandboxAvailability,
  decideSandboxRelaunch,
  isSandboxRelaunched,
} = require('../../electron/linux-sandbox');

// An fs.Stats look-alike: only uid and mode are read.
function helperStat({ uid, mode }) {
  return { uid, mode };
}

const REGULAR_FILE = 0o100000;

test('the profile electron-builder installs for the executable makes the namespace sandbox available', () => {
  // What /proc/self/attr/current reads for /opt/Mind/minds under the
  // .deb's `profile "minds" "/opt/Mind/minds" flags=(unconfined) { userns, }`.
  const availability = sandboxAvailability({
    apparmorLabel: 'minds (unconfined)\n',
    profileName: 'minds',
    sandboxHelperStat: helperStat({ uid: 0, mode: REGULAR_FILE | 0o755 }),
  });
  assert.equal(availability, 'apparmor-profile');
});

test('an unconfined process with a non-setuid helper has no sandbox', () => {
  const availability = sandboxAvailability({
    apparmorLabel: 'unconfined\n',
    profileName: 'minds',
    sandboxHelperStat: helperStat({ uid: 0, mode: REGULAR_FILE | 0o755 }),
  });
  assert.equal(availability, null);
});

test('a stacked profile label does not count as the profile', () => {
  // `aa-exec -p minds` yields this label, and under it user namespaces are
  // still restricted; only an exec of the attached path gets the bare label.
  const availability = sandboxAvailability({
    apparmorLabel: 'minds//&unconfined (unconfined)\n',
    profileName: 'minds',
    sandboxHelperStat: null,
  });
  assert.equal(availability, null);
});

test("another executable's profile does not count either", () => {
  const availability = sandboxAvailability({
    apparmorLabel: 'chrome (unconfined)\n',
    profileName: 'minds',
    sandboxHelperStat: null,
  });
  assert.equal(availability, null);
});

test('a root-owned 4755 helper makes the setuid sandbox available without any profile', () => {
  const availability = sandboxAvailability({
    apparmorLabel: 'unconfined\n',
    profileName: 'minds',
    sandboxHelperStat: helperStat({ uid: 0, mode: REGULAR_FILE | 0o4755 }),
  });
  assert.equal(availability, 'setuid-helper');
});

test('a setuid helper Chromium refuses is not a sandbox: not root-owned, or not world-executable', () => {
  const unconfined = { apparmorLabel: 'unconfined\n', profileName: 'minds' };
  assert.equal(
    sandboxAvailability({ ...unconfined, sandboxHelperStat: helperStat({ uid: 1000, mode: REGULAR_FILE | 0o4755 }) }),
    null,
  );
  assert.equal(
    sandboxAvailability({ ...unconfined, sandboxHelperStat: helperStat({ uid: 0, mode: REGULAR_FILE | 0o4750 }) }),
    null,
  );
});

test('an unreadable label and a missing helper mean no sandbox', () => {
  assert.equal(sandboxAvailability({ apparmorLabel: null, profileName: 'minds', sandboxHelperStat: null }), null);
});

const LINUX_PACKAGED = { platform: 'linux', isPackaged: true };
const PROFILE_AVAILABLE = () => 'apparmor-profile';
const NOTHING_AVAILABLE = () => null;

test('the launcher flag is dropped and the marker added when a sandbox is available', () => {
  const decision = decideSandboxRelaunch({
    ...LINUX_PACKAGED,
    args: [NO_SANDBOX_SWITCH, 'minds://create?git_url=x'],
    detectAvailability: PROFILE_AVAILABLE,
  });
  assert.equal(decision.action, 'relaunch');
  assert.deepEqual(decision.args, ['minds://create?git_url=x', RELAUNCHED_SWITCH]);
  assert.match(decision.reason, /apparmor-profile/);
});

test('a launch without the flag is left alone, without probing the system', () => {
  let probes = 0;
  const decision = decideSandboxRelaunch({
    ...LINUX_PACKAGED,
    args: ['minds://'],
    detectAvailability: () => {
      probes += 1;
      return 'apparmor-profile';
    },
  });
  assert.equal(decision.action, 'keep');
  assert.equal(probes, 0);
});

test('no available sandbox means the flag stays, since Chromium would abort without it', () => {
  const decision = decideSandboxRelaunch({
    ...LINUX_PACKAGED,
    args: [NO_SANDBOX_SWITCH],
    detectAvailability: NOTHING_AVAILABLE,
  });
  assert.equal(decision.action, 'keep');
});

test('a process that was already relaunched never relaunches again', () => {
  // Even with the flag back and a sandbox in sight: one relaunch is the
  // budget, or a launcher that re-adds the flag would loop forever.
  const decision = decideSandboxRelaunch({
    ...LINUX_PACKAGED,
    args: [NO_SANDBOX_SWITCH, RELAUNCHED_SWITCH],
    detectAvailability: PROFILE_AVAILABLE,
  });
  assert.equal(decision.action, 'keep');
  assert.match(decision.reason, /already relaunched/);
});

test('only packaged Linux builds are considered', () => {
  const args = [NO_SANDBOX_SWITCH];
  assert.equal(
    decideSandboxRelaunch({ platform: 'darwin', isPackaged: true, args, detectAvailability: PROFILE_AVAILABLE }).action,
    'keep',
  );
  assert.equal(
    decideSandboxRelaunch({ platform: 'linux', isPackaged: false, args, detectAvailability: PROFILE_AVAILABLE }).action,
    'keep',
  );
});

test('the relaunched process recognizes itself by the marker', () => {
  assert.equal(isSandboxRelaunched(['minds://', RELAUNCHED_SWITCH]), true);
  assert.equal(isSandboxRelaunched([NO_SANDBOX_SWITCH]), false);
});
