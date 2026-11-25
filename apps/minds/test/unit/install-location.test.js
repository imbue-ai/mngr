// Unit tests for the Applications-folder launch gate.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');

const { isLaunchRefused, offerMoveToApplications } = require('../../electron/install-location');

const PACKAGED_MAC = { platform: 'darwin', isPackaged: true, isSmokeTest: false };

test('a packaged macOS app outside an Applications folder is refused', () => {
  assert.equal(isLaunchRefused({ ...PACKAGED_MAC, isInApplicationsFolder: () => false }), true);
  assert.equal(isLaunchRefused({ ...PACKAGED_MAC, isInApplicationsFolder: () => true }), false);
});

test('dev runs, ToDesktop smoke tests, and other platforms never read the location', () => {
  const unreadable = () => {
    throw new Error('isInApplicationsFolder must not be called');
  };
  for (const launch of [
    { ...PACKAGED_MAC, isPackaged: false },
    { ...PACKAGED_MAC, isSmokeTest: true },
    { ...PACKAGED_MAC, platform: 'linux' },
    { ...PACKAGED_MAC, platform: 'win32' },
  ]) {
    assert.equal(isLaunchRefused({ ...launch, isInApplicationsFolder: unreadable }), false, JSON.stringify(launch));
  }
});

// Electron's `app` and `dialog` as far as the gate uses them. `answer` picks
// the button for each dialog in turn; every dialog and call is recorded.
function fakeElectron({ answer, move }) {
  const calls = [];
  const dialog = {
    showMessageBox: async (options) => {
      calls.push({ dialog: options });
      return { response: answer(options) };
    },
  };
  const app = {
    quit: () => calls.push('quit'),
    moveToApplicationsFolder: () => {
      calls.push('move');
      return move();
    },
  };
  return { app, dialog, calls };
}

function clicking(label) {
  return (options) => {
    const index = options.buttons.indexOf(label);
    assert.notEqual(index, -1, `no "${label}" button in ${JSON.stringify(options.buttons)}`);
    return index;
  };
}

const neverMoves = () => assert.fail('moved');

test('Quit quits without moving anything', async () => {
  const fake = fakeElectron({ answer: clicking('Quit'), move: neverMoves });
  await offerMoveToApplications(fake);
  assert.deepEqual(fake.calls.slice(1), ['quit']);
});

test('dismissing the dialog counts as Quit', async () => {
  const fake = fakeElectron({ answer: (options) => options.cancelId, move: neverMoves });
  await offerMoveToApplications(fake);
  assert.equal(fake.calls[0].dialog.buttons[fake.calls[0].dialog.cancelId], 'Quit');
  assert.deepEqual(fake.calls.slice(1), ['quit']);
});

test('Move hands over to Electron\'s move, which quits and relaunches on its own', async () => {
  const fake = fakeElectron({ answer: clicking('Move to Applications'), move: () => true });
  await offerMoveToApplications(fake);
  assert.deepEqual(fake.calls.slice(1), ['move']);
});

for (const [name, move, expectedReason] of [
  [
    'throws',
    () => {
      throw new Error('Failed to copy current bundle to the applications folder');
    },
    'Failed to copy current bundle to the applications folder',
  ],
  ['returns false', () => false, 'The move was cancelled'],
]) {
  test(`a move that ${name} says why and how to move by hand, then quits`, async () => {
    const fake = fakeElectron({
      answer: (options) => (options.type === 'error' ? 0 : clicking('Move to Applications')(options)),
      move,
    });
    await offerMoveToApplications(fake);
    assert.equal(fake.calls[1], 'move');
    const failure = fake.calls[2].dialog;
    assert.equal(failure.type, 'error');
    assert.ok(failure.detail.startsWith(`${expectedReason}.`), failure.detail);
    assert.deepEqual(failure.buttons, ['Quit']);
    assert.deepEqual(fake.calls.slice(3), ['quit']);
  });
}
