'use strict';

// Unit tests for Imbue Studio's own shortcuts in main (electron/app-shortcuts.js).

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {
  APP_SHORTCUT_DEFAULTS,
  APP_SHORTCUT_IDS,
  acceleratorOf,
  menuAcceleratorOf,
  readAppShortcuts,
} = require('../../electron/app-shortcuts');

const FIXTURE = require(path.join(
  __dirname, '..', '..', 'imbue', 'minds', 'desktop_client', 'workspace_profile_contract.json',
));

function withChords(overrides) {
  return { ...Object.fromEntries(Object.entries(APP_SHORTCUT_DEFAULTS).map(([id, chords]) => [id, [...chords]])), ...overrides };
}

test('every named key of the grammar fixture has a menu accelerator', () => {
  assert.deepEqual(FIXTURE.chord_named_keys.filter((key) => acceleratorOf(`meta+${key}`) === null), []);
});

test('the catalog is the six actions of the spec, each with its default', () => {
  assert.deepEqual(APP_SHORTCUT_IDS, ['new-window', 'close-window', 'quit', 'zoom-in', 'zoom-out', 'zoom-reset']);
  assert.deepEqual(APP_SHORTCUT_DEFAULTS['close-window'], ['meta+w']);
});

test('a report naming every action is read, an emptied action included', () => {
  assert.deepEqual(readAppShortcuts(withChords({ quit: [] })), withChords({ quit: [] }));
});

test('a report missing an action, naming an unknown one, or not shaped as chords is refused', () => {
  const missing = withChords({});
  delete missing.quit;
  assert.equal(readAppShortcuts(missing), null);
  assert.equal(readAppShortcuts({ ...withChords({}), minimize: ['meta+m'] }), null);
  assert.equal(readAppShortcuts(withChords({ quit: 'meta+q' })), null);
  assert.equal(readAppShortcuts(withChords({ quit: [7] })), null);
  assert.equal(readAppShortcuts(withChords({ quit: [''] })), null);
  assert.equal(readAppShortcuts(null), null);
  assert.equal(readAppShortcuts([]), null);
});

test('a chord is spelled as an accelerator in Electron\'s names', () => {
  assert.equal(acceleratorOf('meta+n'), 'Cmd+N');
  assert.equal(acceleratorOf('shift+meta+w'), 'Shift+Cmd+W');
  assert.equal(acceleratorOf('meta+='), 'Cmd+=');
  assert.equal(acceleratorOf('meta+-'), 'Cmd+-');
  assert.equal(acceleratorOf('ctrl+alt+arrowup'), 'Ctrl+Alt+Up');
  assert.equal(acceleratorOf('meta+plus'), 'Cmd+Plus');
  assert.equal(acceleratorOf('escape'), 'Esc');
  assert.equal(acceleratorOf('alt+meta+]'), 'Alt+Cmd+]');
});

test('a chord with no accelerator spelling has none, and a menu item takes the first that does', () => {
  assert.equal(acceleratorOf('meta+é'), null);
  assert.equal(acceleratorOf('hyper+n'), null);
  assert.equal(acceleratorOf(''), null);
  assert.equal(menuAcceleratorOf(['meta+é', 'meta+n']), 'Cmd+N');
  assert.equal(menuAcceleratorOf([]), undefined);
});
