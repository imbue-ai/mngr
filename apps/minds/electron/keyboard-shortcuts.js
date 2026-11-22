// Keyboard shortcut preference: the chord that opens the workspace switcher.
//
// Deliberately free of any `electron` import (paths.js pulls in `app`) so the
// rules are unit-testable under plain node -- the caller passes the data
// directory and the platform in.

const fs = require('fs');
const path = require('path');

const PREFERENCE_FILENAME = 'keyboard-shortcuts.json';

// A chord is { code, key, meta, control, alt, shift }: the physical key's
// `code`, the lowercased `key` it produced when recorded, and the modifiers,
// named as Electron's `before-input-event` input names them. `meta` is Cmd on
// macOS and the Super (Windows) key elsewhere.
const MODIFIERS = Object.freeze(['meta', 'control', 'alt', 'shift']);

// `key` values (lowercased) of the keys that only modify another key.
const MODIFIER_KEYS = new Set([
  'shift',
  'control',
  'alt',
  'meta',
  'altgraph',
  'capslock',
  'numlock',
  'scrolllock',
  'fn',
  'fnlock',
  'hyper',
  'super',
  'symbol',
  'symbollock',
  'os',
]);

// `key` values (lowercased) that many physical keys share, so a chord matched
// by `key` cannot be told apart from its neighbours by one of these.
const UNIDENTIFYING_KEYS = new Set(['dead', 'unidentified', 'process']);

function isMacPlatform(platform) {
  return platform === 'darwin';
}

function chordOf(code, key, heldModifiers) {
  const chord = { code, key };
  for (const modifier of MODIFIERS) chord[modifier] = heldModifiers.includes(modifier);
  return Object.freeze(chord);
}

const MAC_DEFAULT_CHORD = chordOf('KeyK', 'k', ['meta']);
const OTHER_DEFAULT_CHORD = chordOf('KeyK', 'k', ['control', 'shift']);

function defaultWorkspaceSwitcherChord(platform) {
  return isMacPlatform(platform) ? MAC_DEFAULT_CHORD : OTHER_DEFAULT_CHORD;
}

/**
 * Resolve a raw value to a chord with every field present, or null for
 * anything that is not shaped like one. A modifier that is absent reads as
 * false; one that is present must be a boolean.
 */
function normalizeChord(raw) {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) return null;
  if (typeof raw.code !== 'string' || raw.code === '') return null;
  if (typeof raw.key !== 'string' || raw.key === '') return null;
  const chord = { code: raw.code, key: raw.key.toLowerCase() };
  for (const modifier of MODIFIERS) {
    const held = raw[modifier];
    if (held !== undefined && typeof held !== 'boolean') return null;
    chord[modifier] = held === true;
  }
  return chord;
}

function isSameChord(a, b) {
  return a.code === b.code && a.key === b.key && MODIFIERS.every((modifier) => a[modifier] === b[modifier]);
}

// Option on macOS changes the character a key produces, so a chord holding it
// is told by its physical key instead.
function isMatchedByCode(chord, platform) {
  return isMacPlatform(platform) && chord.alt;
}

/**
 * Whether a `before-input-event` input ({ key, code, meta, control, alt,
 * shift }) is the chord being pressed. A null chord (the binding is off)
 * matches nothing.
 */
function matchesChord(chord, input, platform) {
  if (chord === null) return false;
  if (!MODIFIERS.every((modifier) => chord[modifier] === (input[modifier] === true))) return false;
  if (isMatchedByCode(chord, platform)) return chord.code === input.code;
  return typeof input.key === 'string' && chord.key === input.key.toLowerCase();
}

/**
 * The chord an input is, or null for a modifier key pressed on its own (and
 * for an input that names no key).
 */
function chordFromInput(input) {
  const chord = normalizeChord({
    code: input.code,
    key: input.key,
    meta: input.meta === true,
    control: input.control === true,
    alt: input.alt === true,
    shift: input.shift === true,
  });
  if (chord === null || MODIFIER_KEYS.has(chord.key)) return null;
  return chord;
}

// Whether the chord's key is one a binding can be told by: not a modifier,
// and not a `key` shared between keys unless the chord goes by its `code`.
function hasBindableKey(chord, platform) {
  if (MODIFIER_KEYS.has(chord.key)) return false;
  return isMatchedByCode(chord, platform) || !UNIDENTIFYING_KEYS.has(chord.key);
}

/** One non-modifier key plus at least one of Cmd, Ctrl, Alt or Super. */
function isValidChord(chord, platform) {
  return hasBindableKey(chord, platform) && (chord.meta || chord.control || chord.alt);
}

// A reserved chord, with the modifiers its handler does not look at: the
// chord is taken whichever way those are held.
function reserve(command, code, key, heldModifiers, ignoredModifiers = []) {
  return Object.freeze({
    command,
    chord: chordOf(code, key, heldModifiers),
    ignoredModifiers: Object.freeze(ignoredModifiers),
  });
}

// The chords a binding may never take. Each list is, in order: what
// `registerShortcutsFor` in main.js handles itself; the accelerators of the
// application menu `installApplicationMenu` builds (macOS only: no menu is
// installed elsewhere), its role items included; and the platform's
// text-editing commands, so a binding can never take copy, paste, undo or
// select-all away from a field. Keep them in step with main.js.
const MAC_RESERVED_CHORDS = Object.freeze([
  reserve('Escape', 'Escape', 'escape', [], MODIFIERS),
  reserve('Toggle Developer Tools', 'KeyI', 'i', ['meta', 'alt'], ['control', 'shift']),
  reserve('Close Workspace Window', 'KeyW', 'w', ['meta']),
  reserve('Quit', 'KeyQ', 'q', ['meta'], ['control']),
  reserve('New Window', 'KeyN', 'n', ['meta'], ['control']),

  reserve('Hide', 'KeyH', 'h', ['meta']),
  reserve('Hide Others', 'KeyH', 'h', ['meta', 'alt']),
  reserve('Actual Size', 'Digit0', '0', ['meta']),
  // Electron's "Plus" accelerator is Shift+=. The unshifted key is kept with
  // it: it is the chord browsers zoom in on.
  reserve('Zoom In', 'Equal', '+', ['meta', 'shift']),
  reserve('Zoom In', 'Equal', '=', ['meta']),
  reserve('Zoom Out', 'Minus', '-', ['meta']),
  reserve('Toggle Full Screen', 'KeyF', 'f', ['meta', 'control']),
  reserve('Minimize', 'KeyM', 'm', ['meta']),

  reserve('Undo', 'KeyZ', 'z', ['meta']),
  reserve('Redo', 'KeyZ', 'z', ['meta', 'shift']),
  reserve('Cut', 'KeyX', 'x', ['meta']),
  reserve('Copy', 'KeyC', 'c', ['meta']),
  reserve('Paste', 'KeyV', 'v', ['meta']),
  reserve('Paste and Match Style', 'KeyV', 'v', ['meta', 'alt', 'shift']),
  reserve('Select All', 'KeyA', 'a', ['meta']),
]);

const OTHER_RESERVED_CHORDS = Object.freeze([
  reserve('Escape', 'Escape', 'escape', [], MODIFIERS),
  reserve('Toggle Developer Tools', 'KeyC', 'c', ['control', 'shift'], ['meta', 'alt']),
  reserve('Close Workspace Window', 'KeyW', 'w', ['control']),
  reserve('Quit', 'KeyQ', 'q', ['control'], ['meta']),
  reserve('New Window', 'KeyN', 'n', ['control'], ['meta']),

  reserve('Undo', 'KeyZ', 'z', ['control']),
  reserve('Redo', 'KeyZ', 'z', ['control', 'shift']),
  reserve('Redo', 'KeyY', 'y', ['control']),
  reserve('Cut', 'KeyX', 'x', ['control']),
  reserve('Copy', 'KeyC', 'c', ['control']),
  reserve('Copy', 'Insert', 'insert', ['control']),
  reserve('Paste', 'KeyV', 'v', ['control']),
  reserve('Paste as Plain Text', 'KeyV', 'v', ['control', 'shift']),
  reserve('Select All', 'KeyA', 'a', ['control']),
]);

function reservedChords(platform) {
  return isMacPlatform(platform) ? MAC_RESERVED_CHORDS : OTHER_RESERVED_CHORDS;
}

/**
 * The reserved entry a chord collides with, or null.
 *
 * main.js tells some of its chords by `code` and the rest, like the menu, by
 * the character, and the two disagree on a keyboard layout that is not
 * QWERTY. A chord collides when either one is the same.
 */
function findReservedChord(chord, platform) {
  const collides = (entry) =>
    (entry.chord.code === chord.code || entry.chord.key === chord.key) &&
    MODIFIERS.every((modifier) => entry.ignoredModifiers.includes(modifier) || entry.chord[modifier] === chord[modifier]);
  return reservedChords(platform).find(collides) || null;
}

const MAC_MODIFIER_GLYPHS = Object.freeze([
  ['control', '⌃'],
  ['alt', '⌥'],
  ['shift', '⇧'],
  ['meta', '⌘'],
]);

function modifierWords(platform) {
  return [
    ['control', 'Ctrl'],
    ['alt', 'Alt'],
    ['shift', 'Shift'],
    ['meta', platform === 'win32' ? 'Win' : 'Super'],
  ];
}

const MAC_KEY_GLYPHS_BY_CODE = new Map([
  ['Enter', '↩'],
  ['Tab', '⇥'],
  ['Backspace', '⌫'],
  ['Delete', '⌦'],
  ['ArrowUp', '↑'],
  ['ArrowDown', '↓'],
  ['ArrowLeft', '←'],
  ['ArrowRight', '→'],
]);

const KEY_NAMES_BY_CODE = new Map([
  ['ArrowUp', 'Up'],
  ['ArrowDown', 'Down'],
  ['ArrowLeft', 'Left'],
  ['ArrowRight', 'Right'],
  ['NumpadEnter', 'Enter'],
  ['Minus', '-'],
  ['Equal', '='],
  ['BracketLeft', '['],
  ['BracketRight', ']'],
  ['Backslash', '\\'],
  ['Semicolon', ';'],
  ['Quote', "'"],
  ['Comma', ','],
  ['Period', '.'],
  ['Slash', '/'],
  ['Backquote', '`'],
]);

function keyLabel(chord, platform) {
  const isCharacter = Array.from(chord.key).length === 1 && chord.key !== ' ';
  if (isCharacter && !isMatchedByCode(chord, platform)) return chord.key.toUpperCase();
  const letterOrDigit = /^(?:Key|Digit)([A-Z0-9])$/.exec(chord.code);
  if (letterOrDigit) return letterOrDigit[1];
  if (isMacPlatform(platform) && MAC_KEY_GLYPHS_BY_CODE.has(chord.code)) return MAC_KEY_GLYPHS_BY_CODE.get(chord.code);
  return KEY_NAMES_BY_CODE.get(chord.code) || chord.code;
}

/** "⌘K" on macOS, in the order its menus draw the glyphs; "Ctrl+Shift+K" elsewhere. */
function chordLabel(chord, platform) {
  const key = keyLabel(chord, platform);
  if (isMacPlatform(platform)) {
    return MAC_MODIFIER_GLYPHS.filter(([modifier]) => chord[modifier]).map(([, glyph]) => glyph).join('') + key;
  }
  return [...modifierWords(platform).filter(([modifier]) => chord[modifier]).map(([, word]) => word), key].join('+');
}

function modifierRequirement(platform) {
  if (isMacPlatform(platform)) return 'Cmd, Ctrl or Option';
  return platform === 'win32' ? 'Ctrl, Alt or Win' : 'Ctrl, Alt or Super';
}

/**
 * Check a raw value as a binding: `chord` is the normalized chord when it may
 * be bound, and `rejection` is otherwise a sentence the user can read.
 */
function checkChord(raw, platform) {
  const chord = normalizeChord(raw);
  if (chord === null) {
    return { chord: null, rejection: 'That is not a keyboard shortcut.' };
  }
  if (!hasBindableKey(chord, platform)) {
    return { chord: null, rejection: 'That key cannot be used in a shortcut.' };
  }
  if (!isValidChord(chord, platform)) {
    return { chord: null, rejection: `A shortcut needs ${modifierRequirement(platform)} held down with one other key.` };
  }
  const reserved = findReservedChord(chord, platform);
  if (reserved !== null) {
    return { chord: null, rejection: `${chordLabel(chord, platform)} is already used for ${reserved.command}.` };
  }
  return { chord, rejection: null };
}

/**
 * What a `set-workspace-switcher-shortcut` request asks to bind: a chord,
 * null for off, or the string "default" for the platform's own.
 */
function resolveWorkspaceSwitcherRequest(value, platform) {
  if (value === null) return { chord: null, rejection: null };
  if (value === 'default') return { chord: defaultWorkspaceSwitcherChord(platform), rejection: null };
  return checkChord(value, platform);
}

function preferencePath(dataDir) {
  return path.join(dataDir, PREFERENCE_FILENAME);
}

/**
 * Read the stored binding (a chord, or null for off), falling back to the
 * platform default when the file is missing, unreadable, or carries a chord
 * that may not be bound.
 *
 * `reason` says why the fallback was used, or is null on a clean read (and on
 * a missing file, which is every install before the binding is first changed).
 */
function readWorkspaceSwitcherChord(dataDir, platform) {
  const fallback = defaultWorkspaceSwitcherChord(platform);
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(preferencePath(dataDir), 'utf8'));
  } catch (err) {
    const reason = err.code === 'ENOENT' ? null : `unreadable (${err.message})`;
    return { chord: fallback, reason };
  }
  if (raw === null || typeof raw !== 'object' || !Object.hasOwn(raw, 'workspace_switcher')) {
    return { chord: fallback, reason: 'has no workspace_switcher binding' };
  }
  const stored = raw.workspace_switcher;
  if (stored === null) return { chord: null, reason: null };
  const checked = checkChord(stored, platform);
  if (checked.rejection !== null) {
    return { chord: fallback, reason: `unusable binding ${JSON.stringify(stored)} (${checked.rejection})` };
  }
  return { chord: checked.chord, reason: null };
}

function writeWorkspaceSwitcherChord(dataDir, chord, platform) {
  let stored = null;
  if (chord !== null) {
    const checked = checkChord(chord, platform);
    if (checked.rejection !== null) {
      throw new Error(`Refusing to store shortcut ${JSON.stringify(chord)}: ${checked.rejection}`);
    }
    stored = checked.chord;
  }
  fs.mkdirSync(dataDir, { recursive: true });
  fs.writeFileSync(preferencePath(dataDir), JSON.stringify({ workspace_switcher: stored }, null, 2) + '\n');
}

/** What `get-keyboard-shortcuts` answers and `keyboard-shortcuts-changed` carries. */
function describeKeyboardShortcuts(chord, platform) {
  const fallback = defaultWorkspaceSwitcherChord(platform);
  return {
    workspaceSwitcher: {
      chord,
      label: chord === null ? null : chordLabel(chord, platform),
      defaultLabel: chordLabel(fallback, platform),
      isDefault: chord !== null && isSameChord(chord, fallback),
    },
  };
}

/**
 * One window's chord capture: `begin` answers a promise for the next chord
 * offered, `{ chord }`, or `{ cancelled: true }` on Escape and on `cancel`.
 * The promise always settles: a second `begin` cancels the first.
 */
function createShortcutCapture() {
  let resolvePending = null;

  const settle = (result) => {
    if (resolvePending === null) return;
    const resolve = resolvePending;
    resolvePending = null;
    resolve(result);
  };
  const cancel = () => settle({ cancelled: true });

  return {
    isPending: () => resolvePending !== null,
    begin: () => {
      cancel();
      return new Promise((resolve) => {
        resolvePending = resolve;
      });
    },
    cancel,
    /** A key-down while pending. A modifier on its own leaves the capture waiting for its key. */
    offerKeyDown: (input) => {
      if (input.key === 'Escape') {
        cancel();
        return;
      }
      const chord = chordFromInput(input);
      if (chord !== null) settle({ chord });
    },
  };
}

module.exports = {
  PREFERENCE_FILENAME,
  MODIFIERS,
  defaultWorkspaceSwitcherChord,
  normalizeChord,
  isSameChord,
  matchesChord,
  chordFromInput,
  isValidChord,
  reservedChords,
  findReservedChord,
  chordLabel,
  checkChord,
  resolveWorkspaceSwitcherRequest,
  readWorkspaceSwitcherChord,
  writeWorkspaceSwitcherChord,
  describeKeyboardShortcuts,
  createShortcutCapture,
};
