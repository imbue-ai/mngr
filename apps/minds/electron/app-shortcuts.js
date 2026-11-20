'use strict';

// Imbue Studio's own shortcuts (specs/keyboard-shortcuts/spec.md, section 8):
// the catalog main falls back on until a window's renderer reports its
// account's chords, the check of that report, and the accelerator spelling
// the macOS menu labels its items with. Kept free of any `electron` import so
// it can be unit-tested under plain node.
//
// The catalog mirrors frontend/src/models/appShortcuts.ts, which a frontend
// test pins against this one.

const APP_SHORTCUT_DEFAULTS = Object.freeze({
  'new-window': Object.freeze(['meta+n']),
  'close-window': Object.freeze(['meta+w']),
  quit: Object.freeze(['meta+q']),
  'zoom-in': Object.freeze(['meta+=']),
  'zoom-out': Object.freeze(['meta+-']),
  'zoom-reset': Object.freeze(['meta+0']),
});

const APP_SHORTCUT_IDS = Object.freeze(Object.keys(APP_SHORTCUT_DEFAULTS));

/**
 * A renderer's report (`set-app-shortcuts`), checked: the chords of every
 * catalog action, by id. Null for a payload not shaped that way, one missing
 * an action, or one naming an action the catalog does not have, so a
 * mismatched renderer keeps the defaults rather than half its own chords.
 *
 * @param {unknown} payload
 * @returns {Record<string, string[]>|null}
 */
function readAppShortcuts(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
  const ids = Object.keys(payload);
  if (ids.length !== APP_SHORTCUT_IDS.length || !APP_SHORTCUT_IDS.every((id) => ids.includes(id))) return null;
  const checked = {};
  for (const id of APP_SHORTCUT_IDS) {
    const chords = payload[id];
    if (!Array.isArray(chords) || !chords.every((chord) => typeof chord === 'string' && chord !== '')) return null;
    checked[id] = [...chords];
  }
  return checked;
}

const MODIFIER_ACCELERATORS = Object.freeze({ ctrl: 'Ctrl', alt: 'Alt', shift: 'Shift', meta: 'Cmd' });

// The grammar's named keys (shell-chords.js's NAMED_KEYS) as Electron spells
// them in an accelerator.
const NAMED_KEY_ACCELERATORS = Object.freeze({
  enter: 'Enter',
  escape: 'Esc',
  tab: 'Tab',
  space: 'Space',
  backspace: 'Backspace',
  delete: 'Delete',
  insert: 'Insert',
  home: 'Home',
  end: 'End',
  pageup: 'PageUp',
  pagedown: 'PageDown',
  arrowup: 'Up',
  arrowdown: 'Down',
  arrowleft: 'Left',
  arrowright: 'Right',
  plus: 'Plus',
  f1: 'F1',
  f2: 'F2',
  f3: 'F3',
  f4: 'F4',
  f5: 'F5',
  f6: 'F6',
  f7: 'F7',
  f8: 'F8',
  f9: 'F9',
  f10: 'F10',
  f11: 'F11',
  f12: 'F12',
});

/**
 * The Electron accelerator a macOS menu item shows for the chord, or null
 * when it has none (a key Electron cannot name, or a chord out of grammar).
 *
 * @param {string} chord
 * @returns {string|null}
 */
function acceleratorOf(chord) {
  if (typeof chord !== 'string' || chord === '') return null;
  const tokens = chord.split('+');
  const key = tokens[tokens.length - 1];
  const modifiers = tokens.slice(0, -1).map((token) => MODIFIER_ACCELERATORS[token]);
  if (modifiers.some((modifier) => modifier === undefined)) return null;
  let keyAccelerator = NAMED_KEY_ACCELERATORS[key];
  if (keyAccelerator === undefined) {
    // Electron's accelerator grammar takes a single printable ASCII character
    // as itself; anything else (an accented letter, a composed symbol) has no
    // spelling there.
    if (!/^[!-~]$/.test(key)) return null;
    keyAccelerator = /^[a-z]$/.test(key) ? key.toUpperCase() : key;
  }
  return [...modifiers, keyAccelerator].join('+');
}

/** The first of the chords a menu item can show, or undefined for none. */
function menuAcceleratorOf(chords) {
  for (const chord of chords) {
    const accelerator = acceleratorOf(chord);
    if (accelerator !== null) return accelerator;
  }
  return undefined;
}

module.exports = {
  APP_SHORTCUT_DEFAULTS,
  APP_SHORTCUT_IDS,
  acceleratorOf,
  menuAcceleratorOf,
  readAppShortcuts,
};
