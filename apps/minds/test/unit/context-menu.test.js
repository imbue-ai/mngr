// Unit tests for the right-click (context) menu item logic.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// These use node's built-in test runner (zero extra deps). buildContextMenuTemplate
// is the pure half of the feature, deliberately split out of main.js (which
// can't be required outside Electron) so the "which items, greyed how" decision
// is testable here; main.js only turns the descriptors into a real Menu and
// pops it up.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { buildContextMenuTemplate, registerContextMenuFor } = require('../../electron/context-menu');

test('editable + selection + clipboard: Cut/Copy/Paste/Select All all enabled', () => {
  assert.deepEqual(
    buildContextMenuTemplate({
      isEditable: true,
      selectionText: 'picked',
      editFlags: { canCut: true, canCopy: true, canPaste: true, canSelectAll: true },
    }),
    [
      { role: 'cut', enabled: true },
      { role: 'copy', enabled: true },
      { role: 'paste', enabled: true },
      { type: 'separator' },
      { role: 'selectAll', enabled: true },
    ],
  );
});

test('editable + no selection + empty clipboard: all items shown but greyed', () => {
  assert.deepEqual(
    buildContextMenuTemplate({
      isEditable: true,
      selectionText: '',
      editFlags: { canCut: false, canCopy: false, canPaste: false, canSelectAll: false },
    }),
    [
      { role: 'cut', enabled: false },
      { role: 'copy', enabled: false },
      { role: 'paste', enabled: false },
      { type: 'separator' },
      { role: 'selectAll', enabled: false },
    ],
  );
});

test('editable: Paste follows the clipboard, independent of a selection', () => {
  const items = buildContextMenuTemplate({
    isEditable: true,
    selectionText: '',
    editFlags: { canCut: false, canCopy: false, canPaste: true, canSelectAll: true },
  });
  const byRole = Object.fromEntries(items.filter((i) => i.role).map((i) => [i.role, i.enabled]));
  assert.equal(byRole.paste, true);
  assert.equal(byRole.cut, false);
  assert.equal(byRole.copy, false);
});

test('non-editable + selection: Copy only, and it is enabled', () => {
  assert.deepEqual(
    buildContextMenuTemplate({
      isEditable: false,
      selectionText: 'some transcript text',
      editFlags: { canCut: false, canCopy: true, canPaste: false, canSelectAll: true },
    }),
    [{ role: 'copy', enabled: true }],
  );
});

test('non-editable + selectionText but no canCopy flag: still offers Copy', () => {
  assert.deepEqual(
    buildContextMenuTemplate({ isEditable: false, selectionText: 'picked' }),
    [{ role: 'copy', enabled: true }],
  );
});

test('non-editable + no selection: no menu', () => {
  assert.deepEqual(
    buildContextMenuTemplate({ isEditable: false, selectionText: '', editFlags: { canCopy: false } }),
    [],
  );
});

test('non-editable + whitespace-only selection: no menu', () => {
  assert.deepEqual(
    buildContextMenuTemplate({ isEditable: false, selectionText: '   \n\t ', editFlags: { canCopy: false } }),
    [],
  );
});

test('robustness: missing params and editFlags are handled without throwing', () => {
  assert.deepEqual(buildContextMenuTemplate(undefined), []);
  assert.deepEqual(buildContextMenuTemplate({}), []);
  assert.deepEqual(
    buildContextMenuTemplate({ isEditable: true }),
    [
      { role: 'cut', enabled: false },
      { role: 'copy', enabled: false },
      { role: 'paste', enabled: false },
      { type: 'separator' },
      { role: 'selectAll', enabled: false },
    ],
  );
});

// Menu is injected, so fakes for win / wc / Menu drive the wiring without
// Electron. The real Electron round trip (native popup + clipboard) is covered
// by test/e2e/context-menu.spec.js.

function makeContextMenuHarness({ winDestroyed = false, wcDestroyed = false } = {}) {
  let handler = null;
  const calls = { built: [], popped: [] };
  const wc = {
    on: (event, fn) => {
      if (event === 'context-menu') handler = fn;
    },
    isDestroyed: () => wcDestroyed,
  };
  const win = { isDestroyed: () => winDestroyed };
  const Menu = {
    buildFromTemplate: (template) => {
      calls.built.push(template);
      return { popup: (opts) => calls.popped.push(opts) };
    },
  };
  registerContextMenuFor(win, wc, Menu);
  return {
    calls,
    win,
    hasHandler: () => handler !== null,
    fire: (params) => {
      if (handler === null) throw new Error('no context-menu handler was registered');
      handler({}, params);
    },
  };
}

test('registerContextMenuFor: registers a context-menu listener on the webContents', () => {
  assert.equal(makeContextMenuHarness().hasHandler(), true);
});

test('registerContextMenuFor: a non-empty target builds the template and pops it over the window', () => {
  const h = makeContextMenuHarness();
  const params = { isEditable: true, selectionText: 'x', editFlags: { canCut: true, canCopy: true, canPaste: true, canSelectAll: true } };
  h.fire(params);
  // Item content is buildContextMenuTemplate's contract (covered above); the
  // wiring only has to pass that template through and pop it over the window.
  assert.deepEqual(h.calls.built, [buildContextMenuTemplate(params)]);
  assert.deepEqual(h.calls.popped, [{ window: h.win }]);
});

test('registerContextMenuFor: an empty target (non-editable, no selection) pops nothing', () => {
  const h = makeContextMenuHarness();
  h.fire({ isEditable: false, selectionText: '', editFlags: { canCopy: false } });
  assert.equal(h.calls.built.length, 0);
  assert.equal(h.calls.popped.length, 0);
});

test('registerContextMenuFor: a destroyed window or webContents is a no-op', () => {
  const editableParams = { isEditable: true, editFlags: { canPaste: true } };
  const deadWin = makeContextMenuHarness({ winDestroyed: true });
  deadWin.fire(editableParams);
  assert.equal(deadWin.calls.built.length, 0);
  assert.equal(deadWin.calls.popped.length, 0);
  const deadWc = makeContextMenuHarness({ wcDestroyed: true });
  deadWc.fire(editableParams);
  assert.equal(deadWc.calls.built.length, 0);
  assert.equal(deadWc.calls.popped.length, 0);
});
