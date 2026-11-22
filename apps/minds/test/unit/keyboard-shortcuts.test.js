// Unit tests for the keyboard shortcut preference (the workspace switcher's chord).
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const shortcuts = require('../../electron/keyboard-shortcuts');

const MAC = 'darwin';
const LINUX = 'linux';
const WINDOWS = 'win32';
const PLATFORMS = [MAC, LINUX, WINDOWS];

function tempDataDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'minds-shortcuts-'));
}

function chord(code, key, ...heldModifiers) {
  const result = { code, key };
  for (const modifier of shortcuts.MODIFIERS) result[modifier] = heldModifiers.includes(modifier);
  return result;
}

// A key-down as Electron's before-input-event reports it.
function keyDown(code, key, ...heldModifiers) {
  return { type: 'keyDown', ...chord(code, key, ...heldModifiers) };
}

function subsetsOf(items) {
  return items.reduce((subsets, item) => subsets.concat(subsets.map((subset) => [...subset, item])), [[]]);
}

// Every chord a reserved entry stands for: one per way of holding the
// modifiers its handler ignores.
function variantsOf(entry) {
  return subsetsOf(entry.ignoredModifiers).map((held) => {
    const variant = { ...entry.chord };
    for (const modifier of entry.ignoredModifiers) variant[modifier] = held.includes(modifier);
    return variant;
  });
}

const MAC_DEFAULT = chord('KeyK', 'k', 'meta');
const OTHER_DEFAULT = chord('KeyK', 'k', 'control', 'shift');

test('the default is Cmd+K on macOS and Ctrl+Shift+K elsewhere', () => {
  assert.deepEqual(shortcuts.defaultWorkspaceSwitcherChord(MAC), MAC_DEFAULT);
  assert.deepEqual(shortcuts.defaultWorkspaceSwitcherChord(LINUX), OTHER_DEFAULT);
  assert.deepEqual(shortcuts.defaultWorkspaceSwitcherChord(WINDOWS), OTHER_DEFAULT);
});

test('each platform default may be bound on its own platform', () => {
  for (const platform of PLATFORMS) {
    const fallback = shortcuts.defaultWorkspaceSwitcherChord(platform);
    assert.deepEqual(shortcuts.checkChord(fallback, platform), { chord: fallback, rejection: null });
  }
});

test('normalizeChord reads absent modifiers as false and lowercases the key', () => {
  assert.deepEqual(shortcuts.normalizeChord({ code: 'KeyK', key: 'k', meta: true }), MAC_DEFAULT);
  assert.deepEqual(shortcuts.normalizeChord({ code: 'KeyK', key: 'K', control: true, shift: true }), OTHER_DEFAULT);
  assert.deepEqual(shortcuts.normalizeChord({ code: 'F5', key: 'F5' }), chord('F5', 'f5'));
});

test('normalizeChord drops fields a chord does not have', () => {
  const input = { ...keyDown('KeyK', 'k', 'meta'), isAutoRepeat: false, location: 0 };
  assert.deepEqual(shortcuts.normalizeChord(input), MAC_DEFAULT);
});

test('normalizeChord rejects anything that is not shaped like a chord', () => {
  assert.equal(shortcuts.normalizeChord(null), null);
  assert.equal(shortcuts.normalizeChord(undefined), null);
  assert.equal(shortcuts.normalizeChord('default'), null);
  assert.equal(shortcuts.normalizeChord('CmdOrCtrl+K'), null);
  assert.equal(shortcuts.normalizeChord(75), null);
  assert.equal(shortcuts.normalizeChord([MAC_DEFAULT]), null);
  assert.equal(shortcuts.normalizeChord({}), null);
  assert.equal(shortcuts.normalizeChord({ key: 'k', meta: true }), null);
  assert.equal(shortcuts.normalizeChord({ code: 'KeyK', meta: true }), null);
  assert.equal(shortcuts.normalizeChord({ code: '', key: 'k', meta: true }), null);
  assert.equal(shortcuts.normalizeChord({ code: 'KeyK', key: '', meta: true }), null);
  assert.equal(shortcuts.normalizeChord({ code: 75, key: 'k', meta: true }), null);
  assert.equal(shortcuts.normalizeChord({ code: 'KeyK', key: 'k', meta: 'yes' }), null);
  assert.equal(shortcuts.normalizeChord({ code: 'KeyK', key: 'k', meta: true, shift: 1 }), null);
  assert.equal(shortcuts.normalizeChord({ code: 'KeyK', key: 'k', meta: true, alt: null }), null);
});

test('matchesChord matches the chord by its key and exact modifiers', () => {
  assert.equal(shortcuts.matchesChord(MAC_DEFAULT, keyDown('KeyK', 'k', 'meta'), MAC), true);
  assert.equal(shortcuts.matchesChord(OTHER_DEFAULT, keyDown('KeyK', 'k', 'control', 'shift'), LINUX), true);
  assert.equal(shortcuts.matchesChord(MAC_DEFAULT, keyDown('KeyJ', 'j', 'meta'), MAC), false);
});

test('matchesChord matches a Shift-held key whose key is reported uppercase', () => {
  assert.equal(shortcuts.matchesChord(OTHER_DEFAULT, keyDown('KeyK', 'K', 'control', 'shift'), LINUX), true);
  assert.equal(shortcuts.matchesChord(OTHER_DEFAULT, keyDown('KeyK', 'K', 'control', 'shift'), WINDOWS), true);
});

test('matchesChord refuses an extra, a missing, or a different modifier', () => {
  assert.equal(shortcuts.matchesChord(MAC_DEFAULT, keyDown('KeyK', 'k', 'meta', 'shift'), MAC), false);
  assert.equal(shortcuts.matchesChord(MAC_DEFAULT, keyDown('KeyK', 'k', 'meta', 'control'), MAC), false);
  assert.equal(shortcuts.matchesChord(MAC_DEFAULT, keyDown('KeyK', '˚', 'meta', 'alt'), MAC), false);
  assert.equal(shortcuts.matchesChord(MAC_DEFAULT, keyDown('KeyK', 'k'), MAC), false);
  assert.equal(shortcuts.matchesChord(MAC_DEFAULT, keyDown('KeyK', 'k', 'control'), MAC), false);
  assert.equal(shortcuts.matchesChord(OTHER_DEFAULT, keyDown('KeyK', 'k', 'control'), LINUX), false);
  assert.equal(shortcuts.matchesChord(OTHER_DEFAULT, keyDown('KeyK', 'K', 'control', 'shift', 'alt'), LINUX), false);
  assert.equal(shortcuts.matchesChord(OTHER_DEFAULT, keyDown('KeyK', 'K', 'control', 'shift', 'meta'), LINUX), false);
});

test('matchesChord follows the character, not the physical key, on another layout', () => {
  // Dvorak: the key that types "k" sits where QWERTY has "v".
  assert.equal(shortcuts.matchesChord(MAC_DEFAULT, keyDown('KeyV', 'k', 'meta'), MAC), true);
  assert.equal(shortcuts.matchesChord(MAC_DEFAULT, keyDown('KeyK', 't', 'meta'), MAC), false);
});

test('a chord holding Alt matches by the physical key on macOS, where Option changes the character', () => {
  const recorded = chord('KeyK', '˚', 'meta', 'alt');
  assert.equal(shortcuts.matchesChord(recorded, keyDown('KeyK', '˚', 'meta', 'alt'), MAC), true);
  // The same physical key, whatever character the layout composes for it.
  assert.equal(shortcuts.matchesChord(recorded, keyDown('KeyK', 'Dead', 'meta', 'alt'), MAC), true);
  // The same character from another physical key is not the chord.
  assert.equal(shortcuts.matchesChord(recorded, keyDown('KeyJ', '˚', 'meta', 'alt'), MAC), false);
});

test('a chord holding Alt still matches by the character off macOS', () => {
  const recorded = chord('KeyK', 'k', 'control', 'alt');
  assert.equal(shortcuts.matchesChord(recorded, keyDown('KeyV', 'k', 'control', 'alt'), LINUX), true);
  assert.equal(shortcuts.matchesChord(recorded, keyDown('KeyK', 't', 'control', 'alt'), LINUX), false);
});

test('a binding that is off matches nothing', () => {
  for (const platform of PLATFORMS) {
    assert.equal(shortcuts.matchesChord(null, keyDown('KeyK', 'k', 'meta'), platform), false);
  }
});

test('matchesChord tolerates an input that names no key', () => {
  assert.equal(shortcuts.matchesChord(MAC_DEFAULT, { type: 'keyDown', meta: true }, MAC), false);
});

test('chordFromInput is the chord pressed, with the key lowercased', () => {
  assert.deepEqual(shortcuts.chordFromInput(keyDown('KeyK', 'K', 'control', 'shift')), OTHER_DEFAULT);
  assert.deepEqual(shortcuts.chordFromInput(keyDown('KeyJ', 'j')), chord('KeyJ', 'j'));
});

test('chordFromInput is null for a modifier key pressed on its own', () => {
  assert.equal(shortcuts.chordFromInput(keyDown('MetaLeft', 'Meta', 'meta')), null);
  assert.equal(shortcuts.chordFromInput(keyDown('ControlLeft', 'Control', 'control')), null);
  assert.equal(shortcuts.chordFromInput(keyDown('AltRight', 'Alt', 'alt')), null);
  assert.equal(shortcuts.chordFromInput(keyDown('AltRight', 'AltGraph', 'alt', 'control')), null);
  assert.equal(shortcuts.chordFromInput(keyDown('ShiftLeft', 'Shift', 'shift', 'control')), null);
  assert.equal(shortcuts.chordFromInput(keyDown('CapsLock', 'CapsLock')), null);
  assert.equal(shortcuts.chordFromInput({ type: 'keyDown', meta: true }), null);
});

test('a valid chord is one key plus at least one of Cmd, Ctrl, Alt or Super', () => {
  for (const platform of PLATFORMS) {
    assert.equal(shortcuts.isValidChord(chord('KeyJ', 'j', 'meta'), platform), true);
    assert.equal(shortcuts.isValidChord(chord('KeyJ', 'j', 'control'), platform), true);
    assert.equal(shortcuts.isValidChord(chord('KeyJ', 'j', 'alt'), platform), true);
    assert.equal(shortcuts.isValidChord(chord('KeyJ', 'j', 'control', 'alt', 'shift', 'meta'), platform), true);
    assert.equal(shortcuts.isValidChord(chord('F5', 'f5', 'control'), platform), true);
    assert.equal(shortcuts.isValidChord(chord('Space', ' ', 'control'), platform), true);
  }
});

test('a key on its own or with only Shift is not a valid chord', () => {
  for (const platform of PLATFORMS) {
    assert.equal(shortcuts.isValidChord(chord('KeyJ', 'j'), platform), false);
    assert.equal(shortcuts.isValidChord(chord('KeyJ', 'j', 'shift'), platform), false);
    assert.equal(shortcuts.isValidChord(chord('F5', 'f5'), platform), false);
  }
});

test('modifier keys alone are not a valid chord', () => {
  for (const platform of PLATFORMS) {
    assert.equal(shortcuts.isValidChord(chord('MetaLeft', 'meta', 'meta'), platform), false);
    assert.equal(shortcuts.isValidChord(chord('ShiftLeft', 'shift', 'control', 'shift'), platform), false);
    assert.equal(shortcuts.isValidChord(chord('AltLeft', 'alt', 'control', 'alt'), platform), false);
    assert.equal(shortcuts.isValidChord(chord('ControlRight', 'control', 'control', 'meta'), platform), false);
  }
});

test('a key that names no character is valid only where the chord matches by the physical key', () => {
  for (const key of ['dead', 'unidentified', 'process']) {
    assert.equal(shortcuts.isValidChord(chord('KeyE', key, 'control', 'alt'), LINUX), false);
    assert.equal(shortcuts.isValidChord(chord('KeyE', key, 'meta'), MAC), false);
    assert.equal(shortcuts.isValidChord(chord('KeyE', key, 'meta', 'alt'), MAC), true);
  }
});

test('checkChord rejects a malformed value with a sentence', () => {
  for (const platform of PLATFORMS) {
    assert.deepEqual(shortcuts.checkChord('CmdOrCtrl+K', platform), {
      chord: null,
      rejection: 'That is not a keyboard shortcut.',
    });
  }
});

test('checkChord names the modifiers the platform has when a chord lacks one', () => {
  const bare = chord('KeyJ', 'j', 'shift');
  assert.deepEqual(shortcuts.checkChord(bare, MAC), {
    chord: null,
    rejection: 'A shortcut needs Cmd, Ctrl or Option held down with one other key.',
  });
  assert.deepEqual(shortcuts.checkChord(bare, LINUX), {
    chord: null,
    rejection: 'A shortcut needs Ctrl, Alt or Super held down with one other key.',
  });
  assert.deepEqual(shortcuts.checkChord(bare, WINDOWS), {
    chord: null,
    rejection: 'A shortcut needs Ctrl, Alt or Win held down with one other key.',
  });
});

test('checkChord rejects a key a binding cannot be told by', () => {
  const rejection = 'That key cannot be used in a shortcut.';
  assert.deepEqual(shortcuts.checkChord(chord('KeyE', 'dead', 'control', 'alt'), LINUX), { chord: null, rejection });
  assert.deepEqual(shortcuts.checkChord(chord('KeyE', 'process', 'meta'), MAC), { chord: null, rejection });
  assert.deepEqual(shortcuts.checkChord(chord('MetaLeft', 'meta', 'meta'), MAC), { chord: null, rejection });
  // Told by its physical key on macOS, so the composed key does not matter.
  assert.equal(shortcuts.checkChord(chord('KeyE', 'dead', 'meta', 'alt'), MAC).rejection, null);
});

test('checkChord answers the normalized chord when it may be bound', () => {
  assert.deepEqual(shortcuts.checkChord({ code: 'KeyJ', key: 'J', meta: true, shift: true }, MAC), {
    chord: chord('KeyJ', 'j', 'meta', 'shift'),
    rejection: null,
  });
  // Super is `meta` off macOS, and counts as the chord's modifier there.
  assert.deepEqual(shortcuts.checkChord({ code: 'KeyJ', key: 'j', meta: true }, LINUX), {
    chord: chord('KeyJ', 'j', 'meta'),
    rejection: null,
  });
});

// The reserved chords, pinned: what registerShortcutsFor handles, the
// application menu's accelerators (macOS only), and the text-editing commands.
// The last column lists the modifiers the handler ignores.
const EXPECTED_MAC_RESERVED = [
  ['Escape', 'Escape', ['meta', 'control', 'alt', 'shift']],
  ['Toggle Developer Tools', '⌥⌘I', ['control', 'shift']],
  ['Close Workspace Window', '⌘W', []],
  ['Quit', '⌘Q', ['control']],
  ['New Window', '⌘N', ['control']],
  ['Hide', '⌘H', []],
  ['Hide Others', '⌥⌘H', []],
  ['Actual Size', '⌘0', []],
  ['Zoom In', '⇧⌘+', []],
  ['Zoom In', '⌘=', []],
  ['Zoom Out', '⌘-', []],
  ['Toggle Full Screen', '⌃⌘F', []],
  ['Minimize', '⌘M', []],
  ['Undo', '⌘Z', []],
  ['Redo', '⇧⌘Z', []],
  ['Cut', '⌘X', []],
  ['Copy', '⌘C', []],
  ['Paste', '⌘V', []],
  ['Paste and Match Style', '⌥⇧⌘V', []],
  ['Select All', '⌘A', []],
];

const EXPECTED_OTHER_RESERVED = [
  ['Escape', 'Escape', ['meta', 'control', 'alt', 'shift']],
  ['Toggle Developer Tools', 'Ctrl+Shift+C', ['meta', 'alt']],
  ['Close Workspace Window', 'Ctrl+W', []],
  ['Quit', 'Ctrl+Q', ['meta']],
  ['New Window', 'Ctrl+N', ['meta']],
  ['Undo', 'Ctrl+Z', []],
  ['Redo', 'Ctrl+Shift+Z', []],
  ['Redo', 'Ctrl+Y', []],
  ['Cut', 'Ctrl+X', []],
  ['Copy', 'Ctrl+C', []],
  ['Copy', 'Ctrl+Insert', []],
  ['Paste', 'Ctrl+V', []],
  ['Paste as Plain Text', 'Ctrl+Shift+V', []],
  ['Select All', 'Ctrl+A', []],
];

function describeReserved(platform) {
  return shortcuts.reservedChords(platform).map((entry) => [
    entry.command,
    shortcuts.chordLabel(entry.chord, platform),
    [...entry.ignoredModifiers],
  ]);
}

test('the reserved chords on macOS are the ones main, the menu and text fields use', () => {
  assert.deepEqual(describeReserved(MAC), EXPECTED_MAC_RESERVED);
});

test('the reserved chords off macOS are the ones main and text fields use', () => {
  assert.deepEqual(describeReserved(LINUX), EXPECTED_OTHER_RESERVED);
  assert.deepEqual(shortcuts.reservedChords(WINDOWS), shortcuts.reservedChords(LINUX));
});

test('Escape is reserved whatever modifiers are held', () => {
  for (const platform of PLATFORMS) {
    const escape = shortcuts.reservedChords(platform).find((entry) => entry.command === 'Escape');
    for (const variant of variantsOf(escape)) {
      assert.equal(shortcuts.findReservedChord(variant, platform), escape);
    }
    assert.equal(variantsOf(escape).length, 16);
  }
});

test('every reserved chord is rejected on its platform, however its ignored modifiers are held', () => {
  for (const platform of PLATFORMS) {
    for (const entry of shortcuts.reservedChords(platform)) {
      for (const variant of variantsOf(entry)) {
        const reserved = shortcuts.findReservedChord(variant, platform);
        assert.notEqual(reserved, null, `${platform}: ${JSON.stringify(variant)} should be reserved`);
        const checked = shortcuts.checkChord(variant, platform);
        assert.equal(checked.chord, null, `${platform}: ${JSON.stringify(variant)} should be rejected`);
        if (shortcuts.isValidChord(variant, platform)) {
          const label = shortcuts.chordLabel(variant, platform);
          assert.equal(checked.rejection, `${label} is already used for ${reserved.command}.`);
        }
      }
    }
  }
});

test('a reserved chord is named with its command in the rejection', () => {
  assert.equal(shortcuts.checkChord(chord('KeyC', 'c', 'meta'), MAC).rejection, '⌘C is already used for Copy.');
  assert.equal(shortcuts.checkChord(chord('KeyC', 'c', 'control'), LINUX).rejection, 'Ctrl+C is already used for Copy.');
  assert.equal(
    shortcuts.checkChord(chord('Escape', 'escape', 'control'), LINUX).rejection,
    'Ctrl+Escape is already used for Escape.',
  );
});

test('a chord is reserved when either its character or its physical key is a reserved one', () => {
  // Dvorak: "c" is typed on the key QWERTY calls KeyI, and KeyC types "j".
  assert.equal(shortcuts.findReservedChord(chord('KeyI', 'c', 'control'), LINUX).command, 'Copy');
  assert.equal(shortcuts.findReservedChord(chord('KeyC', 'j', 'control', 'shift'), LINUX).command, 'Toggle Developer Tools');
  // Option composes another character on macOS; the physical key still collides.
  assert.equal(shortcuts.findReservedChord(chord('KeyI', 'ˆ', 'meta', 'alt'), MAC).command, 'Toggle Developer Tools');
  assert.equal(shortcuts.findReservedChord(chord('KeyH', '˙', 'meta', 'alt'), MAC).command, 'Hide Others');
});

test('a reserved key with other modifiers is free', () => {
  assert.equal(shortcuts.findReservedChord(chord('KeyC', 'c', 'meta', 'shift'), MAC), null);
  assert.equal(shortcuts.findReservedChord(chord('KeyW', 'w', 'meta', 'shift'), MAC), null);
  assert.equal(shortcuts.findReservedChord(chord('KeyW', 'w', 'control', 'alt'), LINUX), null);
  assert.equal(shortcuts.findReservedChord(chord('KeyQ', 'q', 'control', 'shift'), LINUX), null);
});

test('a chord reserved on one platform is free on the other when nothing there uses it', () => {
  assert.equal(shortcuts.findReservedChord(chord('KeyH', 'h', 'meta'), LINUX), null);
  assert.equal(shortcuts.findReservedChord(chord('KeyM', 'm', 'meta'), LINUX), null);
  assert.equal(shortcuts.findReservedChord(chord('KeyY', 'y', 'control'), MAC), null);
  assert.equal(shortcuts.findReservedChord(chord('KeyC', 'c', 'control'), MAC), null);
});

test('chordLabel draws glyphs on macOS in the order its menus do', () => {
  assert.equal(shortcuts.chordLabel(MAC_DEFAULT, MAC), '⌘K');
  assert.equal(shortcuts.chordLabel(chord('KeyK', 'k', 'meta', 'shift'), MAC), '⇧⌘K');
  assert.equal(shortcuts.chordLabel(chord('KeyK', 'k', 'meta', 'control'), MAC), '⌃⌘K');
  assert.equal(shortcuts.chordLabel(chord('KeyK', 'k', 'control', 'alt', 'shift', 'meta'), MAC), '⌃⌥⇧⌘K');
});

test('chordLabel names an Option chord on macOS by its physical key', () => {
  assert.equal(shortcuts.chordLabel(chord('KeyK', '˚', 'meta', 'alt'), MAC), '⌥⌘K');
  assert.equal(shortcuts.chordLabel(chord('Digit2', '™', 'alt'), MAC), '⌥2');
  assert.equal(shortcuts.chordLabel(chord('Slash', '÷', 'meta', 'alt'), MAC), '⌥⌘/');
  assert.equal(shortcuts.chordLabel(chord('KeyE', 'dead', 'meta', 'alt'), MAC), '⌥⌘E');
});

test('chordLabel spells modifiers out off macOS', () => {
  assert.equal(shortcuts.chordLabel(OTHER_DEFAULT, LINUX), 'Ctrl+Shift+K');
  assert.equal(shortcuts.chordLabel(OTHER_DEFAULT, WINDOWS), 'Ctrl+Shift+K');
  assert.equal(shortcuts.chordLabel(chord('KeyK', 'k', 'control', 'alt', 'shift', 'meta'), LINUX), 'Ctrl+Alt+Shift+Super+K');
  assert.equal(shortcuts.chordLabel(chord('KeyK', 'k', 'meta'), LINUX), 'Super+K');
  assert.equal(shortcuts.chordLabel(chord('KeyK', 'k', 'meta'), WINDOWS), 'Win+K');
  // Alt does not change the character off macOS, so the label follows the layout.
  assert.equal(shortcuts.chordLabel(chord('KeyV', 'k', 'control', 'alt'), LINUX), 'Ctrl+Alt+K');
});

test('chordLabel names the keys that type no character', () => {
  assert.equal(shortcuts.chordLabel(chord('Space', ' ', 'control'), LINUX), 'Ctrl+Space');
  assert.equal(shortcuts.chordLabel(chord('Space', ' ', 'meta'), MAC), '⌘Space');
  assert.equal(shortcuts.chordLabel(chord('F5', 'f5', 'control'), LINUX), 'Ctrl+F5');
  assert.equal(shortcuts.chordLabel(chord('F5', 'f5', 'meta'), MAC), '⌘F5');
  assert.equal(shortcuts.chordLabel(chord('ArrowUp', 'arrowup', 'control', 'alt'), LINUX), 'Ctrl+Alt+Up');
  assert.equal(shortcuts.chordLabel(chord('ArrowUp', 'arrowup', 'meta', 'shift'), MAC), '⇧⌘↑');
  assert.equal(shortcuts.chordLabel(chord('Enter', 'enter', 'meta'), MAC), '⌘↩');
  assert.equal(shortcuts.chordLabel(chord('Enter', 'enter', 'control'), LINUX), 'Ctrl+Enter');
  assert.equal(shortcuts.chordLabel(chord('PageDown', 'pagedown', 'control'), LINUX), 'Ctrl+PageDown');
});

test('chordLabel shows a punctuation key as its character', () => {
  assert.equal(shortcuts.chordLabel(chord('Comma', ',', 'meta'), MAC), '⌘,');
  assert.equal(shortcuts.chordLabel(chord('Slash', '/', 'control'), LINUX), 'Ctrl+/');
});

test('a missing preference file reads as the platform default with no reason', () => {
  const dataDir = tempDataDir();
  assert.deepEqual(shortcuts.readWorkspaceSwitcherChord(dataDir, MAC), { chord: MAC_DEFAULT, reason: null });
  assert.deepEqual(shortcuts.readWorkspaceSwitcherChord(dataDir, LINUX), { chord: OTHER_DEFAULT, reason: null });
});

test('writeWorkspaceSwitcherChord round-trips through readWorkspaceSwitcherChord', () => {
  const dataDir = path.join(tempDataDir(), 'nested');
  const recorded = chord('KeyJ', 'j', 'control', 'alt');
  shortcuts.writeWorkspaceSwitcherChord(dataDir, recorded, LINUX);
  assert.deepEqual(shortcuts.readWorkspaceSwitcherChord(dataDir, LINUX), { chord: recorded, reason: null });
});

test('the stored file holds the one binding under workspace_switcher', () => {
  const dataDir = tempDataDir();
  shortcuts.writeWorkspaceSwitcherChord(dataDir, { code: 'KeyJ', key: 'J', meta: true }, MAC);
  const stored = JSON.parse(fs.readFileSync(path.join(dataDir, 'keyboard-shortcuts.json'), 'utf8'));
  assert.deepEqual(stored, { workspace_switcher: chord('KeyJ', 'j', 'meta') });
  assert.equal(shortcuts.PREFERENCE_FILENAME, 'keyboard-shortcuts.json');
});

test('a binding turned off round-trips as null, not as the default', () => {
  const dataDir = tempDataDir();
  shortcuts.writeWorkspaceSwitcherChord(dataDir, null, MAC);
  assert.deepEqual(shortcuts.readWorkspaceSwitcherChord(dataDir, MAC), { chord: null, reason: null });
});

test('a stored chord with absent modifiers reads them as false', () => {
  const dataDir = tempDataDir();
  fs.writeFileSync(
    path.join(dataDir, shortcuts.PREFERENCE_FILENAME),
    JSON.stringify({ workspace_switcher: { code: 'KeyJ', key: 'j', meta: true } }),
  );
  assert.deepEqual(shortcuts.readWorkspaceSwitcherChord(dataDir, MAC), { chord: chord('KeyJ', 'j', 'meta'), reason: null });
});

test('an unparseable file reads as the default with a reason', () => {
  const dataDir = tempDataDir();
  fs.writeFileSync(path.join(dataDir, shortcuts.PREFERENCE_FILENAME), '{not json');
  const read = shortcuts.readWorkspaceSwitcherChord(dataDir, MAC);
  assert.deepEqual(read.chord, MAC_DEFAULT);
  assert.match(read.reason, /unreadable/);
});

test('an unreadable file reads as the default with a reason', () => {
  const dataDir = tempDataDir();
  // A directory where the file belongs: the read fails with something other than ENOENT.
  fs.mkdirSync(path.join(dataDir, shortcuts.PREFERENCE_FILENAME));
  const read = shortcuts.readWorkspaceSwitcherChord(dataDir, LINUX);
  assert.deepEqual(read.chord, OTHER_DEFAULT);
  assert.match(read.reason, /unreadable/);
});

test('a file with no workspace_switcher binding reads as the default with a reason', () => {
  for (const content of ['{}', 'null', '[]', '"k"', '7', '{"workspaceSwitcher": null}']) {
    const dataDir = tempDataDir();
    fs.writeFileSync(path.join(dataDir, shortcuts.PREFERENCE_FILENAME), content);
    const read = shortcuts.readWorkspaceSwitcherChord(dataDir, MAC);
    assert.deepEqual(read.chord, MAC_DEFAULT, content);
    assert.match(read.reason, /has no workspace_switcher binding/, content);
  }
});

test('a stored binding that is not a chord reads as the default with a reason', () => {
  for (const stored of ['CmdOrCtrl+K', 7, false, [], {}, { code: 'KeyJ', key: 'j', meta: 'yes' }]) {
    const dataDir = tempDataDir();
    fs.writeFileSync(path.join(dataDir, shortcuts.PREFERENCE_FILENAME), JSON.stringify({ workspace_switcher: stored }));
    const read = shortcuts.readWorkspaceSwitcherChord(dataDir, LINUX);
    assert.deepEqual(read.chord, OTHER_DEFAULT, JSON.stringify(stored));
    assert.match(read.reason, /unusable binding .*That is not a keyboard shortcut\./, JSON.stringify(stored));
  }
});

test('a stored chord that may not be bound reads as the default with a reason', () => {
  const dataDir = tempDataDir();
  const file = path.join(dataDir, shortcuts.PREFERENCE_FILENAME);

  fs.writeFileSync(file, JSON.stringify({ workspace_switcher: chord('KeyJ', 'j') }));
  const modifierless = shortcuts.readWorkspaceSwitcherChord(dataDir, MAC);
  assert.deepEqual(modifierless.chord, MAC_DEFAULT);
  assert.match(modifierless.reason, /unusable binding .*A shortcut needs/);

  fs.writeFileSync(file, JSON.stringify({ workspace_switcher: chord('KeyC', 'c', 'meta') }));
  const reserved = shortcuts.readWorkspaceSwitcherChord(dataDir, MAC);
  assert.deepEqual(reserved.chord, MAC_DEFAULT);
  assert.match(reserved.reason, /unusable binding .*⌘C is already used for Copy\./);
});

test('writeWorkspaceSwitcherChord refuses a chord that may not be bound', () => {
  const dataDir = tempDataDir();
  const file = path.join(dataDir, shortcuts.PREFERENCE_FILENAME);
  assert.throws(() => shortcuts.writeWorkspaceSwitcherChord(dataDir, 'default', MAC), /Refusing to store shortcut "default"/);
  assert.throws(() => shortcuts.writeWorkspaceSwitcherChord(dataDir, chord('KeyJ', 'j'), MAC), /Refusing to store shortcut/);
  assert.throws(
    () => shortcuts.writeWorkspaceSwitcherChord(dataDir, chord('KeyC', 'c', 'control'), LINUX),
    /Refusing to store shortcut .*Ctrl\+C is already used for Copy\./,
  );
  assert.equal(fs.existsSync(file), false);
});

test('resolveWorkspaceSwitcherRequest reads null as off and "default" as the platform default', () => {
  assert.deepEqual(shortcuts.resolveWorkspaceSwitcherRequest(null, MAC), { chord: null, rejection: null });
  assert.deepEqual(shortcuts.resolveWorkspaceSwitcherRequest('default', MAC), { chord: MAC_DEFAULT, rejection: null });
  assert.deepEqual(shortcuts.resolveWorkspaceSwitcherRequest('default', LINUX), { chord: OTHER_DEFAULT, rejection: null });
});

test('resolveWorkspaceSwitcherRequest checks a chord and rejects one that may not be bound', () => {
  assert.deepEqual(shortcuts.resolveWorkspaceSwitcherRequest({ code: 'KeyJ', key: 'J', control: true, shift: true }, LINUX), {
    chord: chord('KeyJ', 'j', 'control', 'shift'),
    rejection: null,
  });
  assert.deepEqual(shortcuts.resolveWorkspaceSwitcherRequest(chord('KeyV', 'v', 'meta'), MAC), {
    chord: null,
    rejection: '⌘V is already used for Paste.',
  });
  assert.deepEqual(shortcuts.resolveWorkspaceSwitcherRequest(undefined, MAC), {
    chord: null,
    rejection: 'That is not a keyboard shortcut.',
  });
  assert.deepEqual(shortcuts.resolveWorkspaceSwitcherRequest('off', MAC), {
    chord: null,
    rejection: 'That is not a keyboard shortcut.',
  });
});

test('describeKeyboardShortcuts reports the default binding', () => {
  assert.deepEqual(shortcuts.describeKeyboardShortcuts(MAC_DEFAULT, MAC), {
    workspaceSwitcher: { chord: MAC_DEFAULT, label: '⌘K', defaultLabel: '⌘K', isDefault: true },
  });
  assert.deepEqual(shortcuts.describeKeyboardShortcuts(OTHER_DEFAULT, LINUX), {
    workspaceSwitcher: { chord: OTHER_DEFAULT, label: 'Ctrl+Shift+K', defaultLabel: 'Ctrl+Shift+K', isDefault: true },
  });
});

test('describeKeyboardShortcuts reports a changed binding beside the default label', () => {
  const recorded = chord('KeyJ', 'j', 'control', 'alt');
  assert.deepEqual(shortcuts.describeKeyboardShortcuts(recorded, LINUX), {
    workspaceSwitcher: { chord: recorded, label: 'Ctrl+Alt+J', defaultLabel: 'Ctrl+Shift+K', isDefault: false },
  });
});

test('describeKeyboardShortcuts reports a binding that is off with no label', () => {
  assert.deepEqual(shortcuts.describeKeyboardShortcuts(null, MAC), {
    workspaceSwitcher: { chord: null, label: null, defaultLabel: '⌘K', isDefault: false },
  });
});

test('a capture resolves with the next chord pressed', async () => {
  const capture = shortcuts.createShortcutCapture();
  assert.equal(capture.isPending(), false);
  const pending = capture.begin();
  assert.equal(capture.isPending(), true);
  capture.offerKeyDown(keyDown('KeyJ', 'J', 'control', 'shift'));
  assert.deepEqual(await pending, { chord: chord('KeyJ', 'j', 'control', 'shift') });
  assert.equal(capture.isPending(), false);
});

test('a modifier pressed on its own does not end a capture', async () => {
  const capture = shortcuts.createShortcutCapture();
  const pending = capture.begin();
  capture.offerKeyDown(keyDown('ControlLeft', 'Control', 'control'));
  capture.offerKeyDown(keyDown('ShiftLeft', 'Shift', 'control', 'shift'));
  assert.equal(capture.isPending(), true);
  capture.offerKeyDown(keyDown('KeyJ', 'J', 'control', 'shift'));
  assert.deepEqual(await pending, { chord: chord('KeyJ', 'j', 'control', 'shift') });
});

test('a capture takes a chord that may not be bound, leaving the refusal to the store', async () => {
  const capture = shortcuts.createShortcutCapture();
  const pending = capture.begin();
  capture.offerKeyDown(keyDown('KeyC', 'c', 'meta'));
  assert.deepEqual(await pending, { chord: chord('KeyC', 'c', 'meta') });
});

test('Escape cancels a capture, with or without modifiers', async () => {
  const capture = shortcuts.createShortcutCapture();
  const plain = capture.begin();
  capture.offerKeyDown(keyDown('Escape', 'Escape'));
  assert.deepEqual(await plain, { cancelled: true });
  assert.equal(capture.isPending(), false);

  const held = capture.begin();
  capture.offerKeyDown(keyDown('Escape', 'Escape', 'shift'));
  assert.deepEqual(await held, { cancelled: true });
});

test('cancel ends a pending capture and is harmless with none pending', async () => {
  const capture = shortcuts.createShortcutCapture();
  capture.cancel();
  assert.equal(capture.isPending(), false);
  const pending = capture.begin();
  capture.cancel();
  assert.deepEqual(await pending, { cancelled: true });
  assert.equal(capture.isPending(), false);
  capture.cancel();
});

test('a second capture cancels the first and takes the next chord itself', async () => {
  const capture = shortcuts.createShortcutCapture();
  const first = capture.begin();
  const second = capture.begin();
  assert.deepEqual(await first, { cancelled: true });
  assert.equal(capture.isPending(), true);
  capture.offerKeyDown(keyDown('KeyJ', 'j', 'meta'));
  assert.deepEqual(await second, { chord: chord('KeyJ', 'j', 'meta') });
});

test('a key-down offered with no capture pending is dropped', async () => {
  const capture = shortcuts.createShortcutCapture();
  capture.offerKeyDown(keyDown('KeyJ', 'j', 'meta'));
  const pending = capture.begin();
  capture.offerKeyDown(keyDown('KeyL', 'l', 'meta'));
  assert.deepEqual(await pending, { chord: chord('KeyL', 'l', 'meta') });
});
