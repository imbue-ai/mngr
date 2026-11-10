'use strict';

// Unit tests for the shortcuts main runs before any page sees a key
// (electron/shell-chords.js).

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {
  HOST_ACTED_CHORDS,
  chordFromInput,
  chordPlatformOf,
  decideChord,
  readShellShortcuts,
} = require('../../electron/shell-chords');

const FIXTURE = require(path.join(
  __dirname, '..', '..', 'imbue', 'minds', 'desktop_client', 'workspace_profile_contract.json',
));

function input(key, fields = {}) {
  return { key, code: '', control: false, alt: false, shift: false, meta: false, ...fields };
}

test('the host-acted chords are the fixture\'s, so the screen warns about exactly the keys main takes', () => {
  for (const platform of ['mac', 'windows', 'linux']) {
    assert.deepEqual(
      [...HOST_ACTED_CHORDS[platform]],
      FIXTURE.host_acted_chords[platform].map((entry) => entry.chord),
    );
  }
});

test('the platform is read off node\'s', () => {
  assert.equal(chordPlatformOf('darwin'), 'mac');
  assert.equal(chordPlatformOf('win32'), 'windows');
  assert.equal(chordPlatformOf('linux'), 'linux');
});

test('on a Mac, Command is meta and Control is ctrl, in the grammar\'s modifier order', () => {
  assert.equal(chordFromInput(input('k', { meta: true, code: 'KeyK' }), 'mac'), 'meta+k');
  assert.equal(chordFromInput(input('K', { meta: true, shift: true, code: 'KeyK' }), 'mac'), 'shift+meta+k');
  assert.equal(chordFromInput(input('Tab', { control: true }), 'mac'), 'ctrl+tab');
  assert.equal(chordFromInput(input(' ', { control: true, code: 'Space' }), 'mac'), 'ctrl+space');
  assert.equal(chordFromInput(input('+', { meta: true }), 'mac'), 'meta+plus');
  assert.equal(chordFromInput(input('F4', { alt: true, code: 'F4' }), 'mac'), 'alt+f4');
});

test('off a Mac, Control is meta and the Windows key makes no chord', () => {
  assert.equal(chordFromInput(input('w', { control: true, code: 'KeyW' }), 'windows'), 'meta+w');
  assert.equal(chordFromInput(input('F4', { alt: true, code: 'F4' }), 'windows'), 'alt+f4');
  assert.equal(chordFromInput(input(' ', { alt: true, code: 'Space' }), 'windows'), 'alt+space');
  assert.equal(chordFromInput(input('w', { meta: true, code: 'KeyW' }), 'linux'), null);
});

test('while Option is held, a printable key is read from the physical key as the US layout types it', () => {
  assert.equal(chordFromInput(input('∑', { alt: true, meta: true, code: 'KeyW' }), 'mac'), 'alt+meta+w');
  assert.equal(chordFromInput(input('‘', { alt: true, meta: true, code: 'BracketRight' }), 'mac'), 'alt+meta+]');
  assert.equal(chordFromInput(input('“', { alt: true, meta: true, code: 'BracketLeft' }), 'mac'), 'alt+meta+[');
  assert.equal(chordFromInput(input('‹', { alt: true, shift: true, code: 'Digit3' }), 'mac'), 'alt+shift+#');
  assert.equal(chordFromInput(input('±', { alt: true, shift: true, code: 'Equal' }), 'mac'), 'alt+shift+plus');
  assert.equal(chordFromInput(input(']', { control: true, alt: true, code: 'BracketRight' }), 'windows'), 'alt+meta+]');
});

test('bare modifiers, unnamed keys, and whitespace other than the Space bar make no chord', () => {
  assert.equal(chordFromInput(input('Meta', { meta: true }), 'mac'), null);
  assert.equal(chordFromInput(input('Shift', { shift: true }), 'windows'), null);
  assert.equal(chordFromInput(input('MediaPlayPause'), 'mac'), null);
  assert.equal(chordFromInput(input(' ', { alt: true }), 'mac'), null);
  assert.equal(chordFromInput(input(' ', { alt: true, code: 'Space' }), 'mac'), 'alt+space');
  assert.equal(chordFromInput(input(''), 'mac'), null);
});

test('a reported payload is read when shaped as the contract says, and refused otherwise', () => {
  assert.deepEqual(
    readShellShortcuts({ chords_by_action: { 'close-window': ['alt+meta+w'] }, claimed_chords: ['f2'] }),
    { chordsByAction: { 'close-window': ['alt+meta+w'] }, claimedChords: ['f2'] },
  );
  assert.equal(readShellShortcuts(null), null);
  assert.equal(readShellShortcuts({ chords_by_action: [], claimed_chords: [] }), null);
  assert.equal(readShellShortcuts({ chords_by_action: { a: 'f2' }, claimed_chords: [] }), null);
  assert.equal(readShellShortcuts({ chords_by_action: {}, claimed_chords: [7] }), null);
});

const SHORTCUTS = {
  chordsByAction: { 'close-window': ['alt+meta+w'], 'open-launcher': ['meta+k', 'alt+f4'] },
  claimedChords: ['alt+meta+w', 'meta+k', 'alt+space', 'f2'],
};

test('a workspace-wide chord runs its action, on every platform', () => {
  assert.deepEqual(
    decideChord({ chord: 'alt+meta+w', platform: 'mac', isRecording: false, shortcuts: SHORTCUTS }),
    { kind: 'run-shortcut', action: 'close-window' },
  );
  // A host-acted key bound to a workspace-wide action runs that action, ahead of the app claim.
  assert.deepEqual(
    decideChord({ chord: 'alt+f4', platform: 'windows', isRecording: false, shortcuts: SHORTCUTS }),
    { kind: 'run-shortcut', action: 'open-launcher' },
  );
});

test('a host-acted key an app claims is taken from the OS for the page; any other claimed key reaches the page', () => {
  assert.deepEqual(
    decideChord({ chord: 'alt+space', platform: 'windows', isRecording: false, shortcuts: SHORTCUTS }),
    { kind: 'run-chord', chord: 'alt+space' },
  );
  assert.equal(decideChord({ chord: 'alt+space', platform: 'mac', isRecording: false, shortcuts: SHORTCUTS }), null);
  assert.equal(decideChord({ chord: 'f2', platform: 'windows', isRecording: false, shortcuts: SHORTCUTS }), null);
});

test('while the recorder listens, a host-acted key goes to it and every other key is left alone', () => {
  assert.deepEqual(
    decideChord({ chord: 'alt+f4', platform: 'windows', isRecording: true, shortcuts: SHORTCUTS }),
    { kind: 'record-chord', chord: 'alt+f4' },
  );
  assert.equal(decideChord({ chord: 'alt+meta+w', platform: 'mac', isRecording: true, shortcuts: SHORTCUTS }), null);
  assert.equal(decideChord({ chord: 'alt+f4', platform: 'windows', isRecording: true, shortcuts: null }).kind, 'record-chord');
});

test('with no shortcuts reported, main matches nothing', () => {
  assert.equal(decideChord({ chord: 'alt+meta+w', platform: 'mac', isRecording: false, shortcuts: null }), null);
  assert.equal(decideChord({ chord: null, platform: 'mac', isRecording: false, shortcuts: SHORTCUTS }), null);
});
