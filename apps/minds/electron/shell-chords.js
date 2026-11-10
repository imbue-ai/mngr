'use strict';

// Pure decisions behind the shortcuts main runs before any page sees a key
// (specs/keyboard-shortcuts/spec.md, section 6): the chord one
// `before-input-event` keystroke spells, and what main does with it. Kept free
// of any `electron` import so it can be unit-tested under plain node; main.js
// hands it the input and acts on the answer.
//
// The chord is read exactly as the workspace's `keybindings.ts` reads a
// keydown: `meta` is the primary modifier (Command on a Mac, Control
// elsewhere, where the Windows key is no modifier and Control never spells
// `ctrl`), and while Option is held a printable key is read from the physical
// key as the US layout types it, since a Mac composes a character for it.

// The keys the grammar names (workspace_profile_contract.json's
// chord_named_keys), lowercased `KeyboardEvent.key` values.
const NAMED_KEYS = new Set([
  'enter', 'escape', 'tab', 'space', 'backspace', 'delete', 'insert', 'home', 'end', 'pageup', 'pagedown',
  'arrowup', 'arrowdown', 'arrowleft', 'arrowright', 'plus',
  'f1', 'f2', 'f3', 'f4', 'f5', 'f6', 'f7', 'f8', 'f9', 'f10', 'f11', 'f12',
]);

// The printable keys of the US layout other than the letters, by
// `KeyboardEvent.code`: the character each types, unshifted and shifted.
const US_LAYOUT_CHARACTERS = Object.freeze({
  Backquote: ['`', '~'],
  Digit1: ['1', '!'],
  Digit2: ['2', '@'],
  Digit3: ['3', '#'],
  Digit4: ['4', '$'],
  Digit5: ['5', '%'],
  Digit6: ['6', '^'],
  Digit7: ['7', '&'],
  Digit8: ['8', '*'],
  Digit9: ['9', '('],
  Digit0: ['0', ')'],
  Minus: ['-', '_'],
  Equal: ['=', '+'],
  BracketLeft: ['[', '{'],
  BracketRight: [']', '}'],
  Backslash: ['\\', '|'],
  Semicolon: [';', ':'],
  Quote: ["'", '"'],
  Comma: [',', '<'],
  Period: ['.', '>'],
  Slash: ['/', '?'],
});

// The keys that reach the app and that the OS then acts on unless main
// intercepts them (spec section 2.4). Mirrors the fixture's
// host_acted_chords, which a unit test pins it against.
const HOST_ACTED_CHORDS = Object.freeze({
  mac: Object.freeze([]),
  windows: Object.freeze(['alt+f4', 'alt+space']),
  linux: Object.freeze([]),
});

/**
 * The chord platform of a Node `process.platform`.
 *
 * @param {string} nodePlatform
 * @returns {'mac'|'windows'|'linux'}
 */
function chordPlatformOf(nodePlatform) {
  if (nodePlatform === 'darwin') return 'mac';
  if (nodePlatform === 'win32') return 'windows';
  return 'linux';
}

function usLayoutCharacter(code, isShifted) {
  const letter = /^Key([A-Z])$/.exec(code);
  if (letter !== null) return letter[1].toLowerCase();
  const characters = US_LAYOUT_CHARACTERS[code];
  return characters === undefined ? null : characters[isShifted ? 1 : 0];
}

function spelledCharacter(character) {
  return character === '+' ? 'plus' : character.toLowerCase();
}

/**
 * The canonical chord one keystroke spells on the platform, or null for a
 * keystroke that is no chord (a bare modifier, a key the grammar has no name
 * for, or off a Mac the Windows key).
 *
 * @param {{key: string, code?: string, control: boolean, alt: boolean, shift: boolean, meta: boolean}} input
 * @param {'mac'|'windows'|'linux'} platform
 * @returns {string|null}
 */
function chordFromInput(input, platform) {
  const raw = typeof input.key === 'string' ? input.key : '';
  if (raw === '' || raw === 'Control' || raw === 'Alt' || raw === 'Shift' || raw === 'Meta') return null;
  const isMac = platform === 'mac';
  if (!isMac && input.meta) return null;
  const code = typeof input.code === 'string' ? input.code : '';
  const physical = input.alt && code !== '' ? usLayoutCharacter(code, input.shift) : null;
  let key;
  if (code === 'Space' || raw === ' ') key = 'space';
  else if (physical !== null) key = spelledCharacter(physical);
  else if (/^\s$/.test(raw)) return null;
  else if (raw.length === 1) key = spelledCharacter(raw);
  else {
    key = raw.toLowerCase();
    if (!NAMED_KEYS.has(key)) return null;
  }
  const tokens = [];
  if (isMac && input.control) tokens.push('ctrl');
  if (input.alt) tokens.push('alt');
  if (input.shift) tokens.push('shift');
  if (isMac ? input.meta : input.control) tokens.push('meta');
  tokens.push(key);
  return tokens.join('+');
}

/**
 * The shortcuts a workspace reported (`minds:shell-shortcuts`), checked: each
 * workspace-wide action's chords by action id, and every chord any of its
 * apps claims. Null for a payload not shaped that way.
 *
 * @param {unknown} payload
 * @returns {{chordsByAction: Record<string, string[]>, claimedChords: string[]}|null}
 */
function readShellShortcuts(payload) {
  if (!payload || typeof payload !== 'object') return null;
  const { chords_by_action: chordsByAction, claimed_chords: claimedChords } = payload;
  if (!chordsByAction || typeof chordsByAction !== 'object' || Array.isArray(chordsByAction)) return null;
  if (!Array.isArray(claimedChords) || !claimedChords.every((chord) => typeof chord === 'string')) return null;
  const checked = {};
  for (const [action, chords] of Object.entries(chordsByAction)) {
    if (!Array.isArray(chords) || !chords.every((chord) => typeof chord === 'string')) return null;
    checked[action] = [...chords];
  }
  return { chordsByAction: checked, claimedChords: [...claimedChords] };
}

/**
 * What main does with a keystroke's chord, in the precedence of spec section
 * 6.6 (immersive mode and the dev-tools key are main's to check first):
 * while the window's shortcut recorder listens, relay a host-acted key to it
 * and let every other key through; otherwise run a workspace-wide action's
 * chord in the workspace, and take a host-acted key an app claims from the OS
 * for the focused page. Null when main leaves the key alone. With no
 * shortcuts reported for the displayed workspace, main matches nothing here.
 *
 * @param {object} options
 * @param {string|null} options.chord
 * @param {'mac'|'windows'|'linux'} options.platform
 * @param {boolean} options.isRecording
 * @param {{chordsByAction: Record<string, string[]>, claimedChords: string[]}|null} options.shortcuts
 * @returns {{kind: 'record-chord', chord: string}|{kind: 'run-shortcut', action: string}|{kind: 'run-chord', chord: string}|null}
 */
function decideChord({ chord, platform, isRecording, shortcuts }) {
  if (chord === null) return null;
  const isHostActed = HOST_ACTED_CHORDS[platform].includes(chord);
  if (isRecording) return isHostActed ? { kind: 'record-chord', chord } : null;
  if (shortcuts === null) return null;
  const action = Object.keys(shortcuts.chordsByAction).find((id) => shortcuts.chordsByAction[id].includes(chord));
  if (action !== undefined) return { kind: 'run-shortcut', action };
  if (isHostActed && shortcuts.claimedChords.includes(chord)) return { kind: 'run-chord', chord };
  return null;
}

module.exports = {
  HOST_ACTED_CHORDS,
  chordFromInput,
  chordPlatformOf,
  decideChord,
  readShellShortcuts,
};
