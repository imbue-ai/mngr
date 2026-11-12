// Unit tests for the right-click (context) menu.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// buildContextMenuTemplate is the pure half of the feature (which items, in
// what order, greyed how, doing what); registerContextMenuFor is the wiring,
// driven here over fake Electron pieces and a fake frame that runs the menu's
// real scripts against a stand-in page. The real Electron round trip (native
// popup, clipboard, a cross-origin frame's rows) is covered by
// test/e2e/context-menu.spec.js.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { buildContextMenuTemplate, readPageMenuSections, registerContextMenuFor } = require('../../electron/context-menu');
const { global: PAGE_MENU_GLOBAL } = require('../../electron/page-menu-bridge.json');

function recordingActions({ unknownSymbols = [] } = {}) {
  const calls = [];
  const record = (name) => (...args) => calls.push([name, ...args]);
  return {
    calls,
    actions: {
      replaceMisspelling: record('replaceMisspelling'),
      addToDictionary: record('addToDictionary'),
      lookUp: record('lookUp'),
      copyText: record('copyText'),
      copyImageAt: record('copyImageAt'),
      inspectAt: record('inspectAt'),
      selectPageRow: record('selectPageRow'),
      menuSymbol: (name) => (unknownSymbols.includes(name) ? null : `icon:${name}`),
    },
  };
}

/** One line per item: a role, a label, or a separator, with its enabled state and icon when set. */
function itemLines(template) {
  return template.map((item) => {
    if (item.type === 'separator') return '---';
    let line = item.role ? `role:${item.role}` : item.label;
    if (item.enabled === false) line += ' (disabled)';
    if (item.icon) line += ` [${item.icon}]`;
    return line;
  });
}

function itemLabelled(template, label) {
  const found = template.find((item) => item.label === label);
  if (found === undefined) throw new Error(`no item labelled ${label}`);
  return found;
}

const EDIT_FLAGS_ALL = { canCut: true, canCopy: true, canPaste: true, canSelectAll: true };

const PAGE_SECTIONS = {
  page: [{ kind: 'action', key: 'rename', label: 'Rename', isDisabled: false, tooltip: null, symbol: 'pencil.line' }],
  link: [{ kind: 'action', key: 'open-link', label: 'Open link', isDisabled: false, tooltip: null, symbol: null }],
  reference: [
    { kind: 'action', key: 'copy-reference', label: 'Copy reference', isDisabled: false, tooltip: null, symbol: 'doc.on.doc' },
    {
      kind: 'action',
      key: 'explain-element',
      label: 'Explain...',
      isDisabled: true,
      tooltip: 'Open this page in the workspace to draft into a chat',
      symbol: 'questionmark.bubble',
    },
  ],
};

/** The template for a right-click, off macOS with no page rows and Inspect Element shown unless a test says otherwise, and the actions its items called. */
function templateFor(params, { pageSections = null, platform = 'linux', showsInspectElement = true, unknownSymbols = [] } = {}) {
  const { actions, calls } = recordingActions({ unknownSymbols });
  return { template: buildContextMenuTemplate(params, { pageSections, platform, showsInspectElement, actions }), calls };
}

test('an editable field off macOS: the edit roles greyed per the edit flags, then Inspect Element', () => {
  const { template } = templateFor({ isEditable: true, selectionText: '', editFlags: { canPaste: true, canSelectAll: true } });
  assert.deepEqual(itemLines(template), [
    'role:cut (disabled)',
    'role:copy (disabled)',
    'role:paste',
    '---',
    'role:selectAll',
    '---',
    'Inspect Element',
  ]);
});

test('selected text on macOS: Look Up quoting it on one line, Copy, then Share, Speech, and Services with their icons', () => {
  const { template, calls } = templateFor(
    { isEditable: false, selectionText: '  a selection\n\tlong enough to be cut short  ', editFlags: { canCopy: true } },
    { platform: 'darwin' },
  );
  assert.deepEqual(itemLines(template), [
    'Look Up “a selection long enough…”',
    '---',
    'role:copy',
    '---',
    'role:shareMenu [icon:square.and.arrow.up]',
    'Speech [icon:text.bubble]',
    'role:services',
    '---',
    'Inspect Element',
  ]);
  assert.deepEqual(template.find((item) => item.role === 'shareMenu').sharingItem, {
    texts: ['a selection\n\tlong enough to be cut short'],
  });
  itemLabelled(template, 'Look Up “a selection long enough…”').click();
  assert.deepEqual(calls, [['lookUp']]);
});

test('Look Up cuts a long selection by character, keeping an emoji at the cut whole', () => {
  const { template } = templateFor(
    { isEditable: false, selectionText: `${'x'.repeat(23)}😀 and more`, editFlags: { canCopy: true } },
    { platform: 'darwin', showsInspectElement: false },
  );
  assert.equal(itemLines(template)[0], `Look Up “${'x'.repeat(23)}😀…”`);
});

test('the same selection off macOS leaves out every macOS-only item', () => {
  const { template } = templateFor({ isEditable: false, selectionText: 'words', editFlags: { canCopy: true } });
  assert.deepEqual(itemLines(template), ['role:copy', '---', 'Inspect Element']);
});

test('a misspelled word: its suggestions first, each replacing the word, and Add to Dictionary', () => {
  const params = {
    isEditable: true,
    misspelledWord: 'teh',
    dictionarySuggestions: ['the', 'ten', 'tech', 'tea', 'tee', 'tel'],
    editFlags: EDIT_FLAGS_ALL,
  };
  const { template, calls } = templateFor(params);
  assert.deepEqual(itemLines(template).slice(0, 7), ['the', 'ten', 'tech', 'tea', 'tee', 'Add to Dictionary', '---']);
  itemLabelled(template, 'tech').click();
  itemLabelled(template, 'Add to Dictionary').click();
  assert.deepEqual(calls, [['replaceMisspelling', 'tech'], ['addToDictionary', 'teh']]);
  const noGuesses = templateFor({ ...params, dictionarySuggestions: [] }).template;
  assert.deepEqual(itemLines(noGuesses).slice(0, 2), ['No Guesses Found (disabled)', 'Add to Dictionary']);
});

test("a page's rows go where they belong among the native items, and a pick runs it by key", () => {
  const { template, calls } = templateFor(
    { isEditable: false, selectionText: '', linkURL: 'https://example.com/docs', editFlags: {} },
    { pageSections: PAGE_SECTIONS, platform: 'darwin', unknownSymbols: ['pencil.line'] },
  );
  assert.deepEqual(itemLines(template), [
    'Rename',
    '---',
    'Open link',
    'Copy Link',
    '---',
    'role:shareMenu [icon:square.and.arrow.up]',
    '---',
    'Copy reference [icon:doc.on.doc]',
    'Explain... (disabled) [icon:questionmark.bubble]',
    '---',
    'Inspect Element',
  ]);
  assert.equal(itemLabelled(template, 'Explain...').toolTip, 'Open this page in the workspace to draft into a chat');
  itemLabelled(template, 'Copy reference').click();
  itemLabelled(template, 'Copy Link').click();
  assert.deepEqual(calls, [['selectPageRow', 'copy-reference'], ['copyText', 'https://example.com/docs']]);
});

test('an image: Copy Image at the click and Copy Image Address', () => {
  const { template, calls } = templateFor({ mediaType: 'image', srcURL: 'https://example.com/a.png', x: 31, y: 47, editFlags: {} });
  assert.deepEqual(itemLines(template), ['Copy Image', 'Copy Image Address', '---', 'Inspect Element']);
  itemLabelled(template, 'Copy Image').click();
  itemLabelled(template, 'Copy Image Address').click();
  itemLabelled(template, 'Inspect Element').click();
  assert.deepEqual(calls, [
    ['copyImageAt', 31, 47],
    ['copyText', 'https://example.com/a.png'],
    ['inspectAt', 31, 47],
  ]);
});

test('a production build leaves Inspect Element out, so a right-click with nothing to offer has no items', () => {
  const editable = templateFor({ isEditable: true, editFlags: EDIT_FLAGS_ALL }, { showsInspectElement: false }).template;
  assert.deepEqual(itemLines(editable), ['role:cut', 'role:copy', 'role:paste', '---', 'role:selectAll']);
  const blank = templateFor(
    { isEditable: false, selectionText: '', editFlags: {} },
    { platform: 'darwin', showsInspectElement: false },
  ).template;
  assert.deepEqual(blank, []);
});

test('readPageMenuSections keeps well-formed rows within bounds and drops the rest', () => {
  const longLabel = 'x'.repeat(500);
  const read = readPageMenuSections({
    page: [
      { kind: 'action', key: 'ok', label: longLabel, isDisabled: 'yes', tooltip: 7, symbol: 'doc.on.doc' },
      { kind: 'action', key: '', label: 'no key' },
      { kind: 'action', key: 'blank', label: '   ' },
      { kind: 'divider' },
      { kind: 'action', key: 'bad-symbol', label: 'Bad symbol', symbol: '../../etc' },
      'not an object',
    ],
    link: Array.from({ length: 50 }, (_, index) => ({ kind: 'action', key: `l${index}`, label: `Link ${index}` })),
  });
  assert.equal(read.page.length, 3);
  assert.deepEqual(read.page[0], {
    kind: 'action',
    key: 'ok',
    label: 'x'.repeat(120),
    isDisabled: false,
    tooltip: null,
    symbol: 'doc.on.doc',
  });
  assert.deepEqual(read.page[1], { kind: 'divider' });
  assert.equal(read.page[2].symbol, null);
  assert.equal(read.link.length, 20);
  assert.deepEqual(read.reference, []);
  const divider = { kind: 'divider' };
  const row = (key) => ({ kind: 'action', key, label: key });
  const tidied = readPageMenuSections({ page: [divider, row('a'), divider, divider, row('b'), divider] });
  assert.deepEqual(
    tidied.page.map((item) => item.key ?? '|'),
    ['a', '|', 'b'],
  );
  const emojiAtCut = readPageMenuSections({
    reference: [{ kind: 'action', key: 'e', label: `${'x'.repeat(119)}😀 and more`, tooltip: `${'y'.repeat(119)}😀!` }],
  }).reference[0];
  assert.equal(emojiAtCut.label, `${'x'.repeat(119)}😀`);
  assert.equal(emojiAtCut.tooltip, `${'y'.repeat(119)}😀`);
  assert.equal(readPageMenuSections(null), null);
  assert.equal(readPageMenuSections('sections'), null);
});

test('readPageMenuSections keeps a submenu one level deep, with its rows checked like a section, and check rows', () => {
  const read = readPageMenuSections({
    page: [
      {
        kind: 'submenu',
        label: 'Move to',
        symbol: 'folder',
        items: [
          { kind: 'divider' },
          { kind: 'action', key: 'move-inbox', label: 'Inbox' },
          { kind: 'check', key: 'move-done', label: 'Done', isChecked: true },
          { kind: 'submenu', label: 'Nested', items: [{ kind: 'action', key: 'deep', label: 'Deep' }] },
          ...Array.from({ length: 30 }, (_, index) => ({ kind: 'action', key: `m${index}`, label: `Item ${index}` })),
        ],
      },
      { kind: 'submenu', label: 'Empty', items: [{ kind: 'action', key: '', label: 'no key' }] },
      { kind: 'submenu', label: '', items: [{ kind: 'action', key: 'a', label: 'A' }] },
      { kind: 'check', key: 'pin', label: 'Pinned', isChecked: 'yes' },
      { kind: 'check', key: '', label: 'No key' },
    ],
  });
  assert.equal(read.page.length, 2);
  const [submenu, check] = read.page;
  assert.equal(submenu.kind, 'submenu');
  assert.equal(submenu.symbol, 'folder');
  assert.deepEqual(submenu.items.slice(0, 2), [
    { kind: 'action', key: 'move-inbox', label: 'Inbox', isDisabled: false, tooltip: null, symbol: null },
    { kind: 'check', key: 'move-done', label: 'Done', isChecked: true, tooltip: null, symbol: null },
  ]);
  assert.equal(submenu.items.length, 18);
  assert.ok(submenu.items.every((item) => item.kind !== 'submenu'));
  assert.deepEqual(check, { kind: 'check', key: 'pin', label: 'Pinned', isChecked: false, tooltip: null, symbol: null });
});

test("a page's submenu opens a native submenu and its check rows are native checkboxes, each running by key", () => {
  const pageSections = readPageMenuSections({
    page: [
      {
        kind: 'submenu',
        label: 'Status',
        symbol: 'flag',
        items: [
          { kind: 'check', key: 'status-open', label: 'Open', isChecked: true },
          { kind: 'action', key: 'status-clear', label: 'Clear', isDisabled: true, tooltip: 'Nothing to clear' },
        ],
      },
      { kind: 'check', key: 'pin', label: 'Pinned', isChecked: false },
    ],
  });
  const { template, calls } = templateFor(
    { isEditable: false, selectionText: '', editFlags: {} },
    { pageSections, platform: 'darwin', showsInspectElement: false },
  );
  assert.deepEqual(itemLines(template), ['Status [icon:flag]', 'Pinned']);
  const status = itemLabelled(template, 'Status');
  assert.deepEqual(
    status.submenu.map((item) => [item.label, item.type ?? 'normal', item.checked ?? null, item.enabled ?? true]),
    [
      ['Open', 'checkbox', true, true],
      ['Clear', 'normal', null, false],
    ],
  );
  assert.equal(status.submenu[1].toolTip, 'Nothing to clear');
  const pinned = itemLabelled(template, 'Pinned');
  assert.equal(pinned.type, 'checkbox');
  assert.equal(pinned.checked, false);
  status.submenu[0].click();
  pinned.click();
  assert.deepEqual(calls, [['selectPageRow', 'status-open'], ['selectPageRow', 'pin']]);
});

// A frame that runs the menu's scripts against a stand-in page: `page` is the
// page's global object, which holds the bridge when the page installed one.
// With `answersLater`, a script's answer waits until `answer()` is called.
function fakeFrame(page, { neverAnswers = false, answersLater = false } = {}) {
  const scripts = [];
  const pending = [];
  return {
    scripts,
    answer: () => pending.splice(0).forEach((resolve) => resolve()),
    isDestroyed: () => false,
    executeJavaScript: (code, userGesture) => {
      scripts.push({ code, userGesture: userGesture === true });
      if (neverAnswers) return new Promise(() => {});
      const run = () => new Function('globalThis', `return ${code};`)(page);
      if (answersLater) return new Promise((resolve) => pending.push(() => resolve(run())));
      return Promise.resolve(run());
    },
  };
}

function pageWithBridge(sections) {
  const selected = [];
  return {
    selected,
    page: { [PAGE_MENU_GLOBAL]: { sections: () => sections, select: async (key) => selected.push(key) } },
  };
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function makeHarness({ winDestroyed = false, showsInspectElement = true } = {}) {
  let handler = null;
  const popped = [];
  const focused = [];
  const wc = {
    on: (event, fn) => {
      if (event === 'context-menu') handler = fn;
    },
    isDestroyed: () => false,
  };
  const win = { isDestroyed: () => winDestroyed, focus: () => focused.push(true) };
  const deps = {
    Menu: {
      buildFromTemplate: (template) => ({ popup: (options) => popped.push({ template, options }) }),
    },
    clipboard: { writeText: () => undefined },
    createMenuSymbol: (name) => ({ isEmpty: () => false, name }),
    platform: 'linux',
    showsInspectElement,
  };
  registerContextMenuFor(win, wc, deps);
  return {
    win,
    popped,
    focused,
    fire: (params) => handler({}, params),
    async waitForPopups(count) {
      for (let attempt = 0; attempt < 200 && popped.length < count; attempt += 1) {
        await delay(5);
      }
      assert.equal(popped.length, count);
    },
  };
}

test("registerContextMenuFor reads the right-clicked frame's rows and pops one menu with them over the window", async () => {
  const h = makeHarness();
  const { page } = pageWithBridge(PAGE_SECTIONS);
  const frame = fakeFrame(page);
  h.fire({ frame, isEditable: false, selectionText: '', editFlags: {} });
  await h.waitForPopups(1);
  assert.deepEqual(itemLines(h.popped[0].template).slice(0, 3), ['Rename', '---', 'Open link']);
  assert.deepEqual(h.popped[0].options, { window: h.win });
  assert.equal(frame.scripts[0].userGesture, false);
});

test('registerContextMenuFor: a picked page row raises the window and runs in its frame with a user gesture', async () => {
  const h = makeHarness();
  const { page, selected } = pageWithBridge(PAGE_SECTIONS);
  const frame = fakeFrame(page);
  h.fire({ frame, editFlags: {} });
  await h.waitForPopups(1);
  itemLabelled(h.popped[0].template, 'Copy reference').click();
  assert.deepEqual(selected, ['copy-reference']);
  assert.equal(frame.scripts[1].userGesture, true);
  assert.deepEqual(h.focused, [true]);
});

test('registerContextMenuFor: a frame with no bridge, or one that never answers, gets the native items alone', async () => {
  const h = makeHarness();
  h.fire({ frame: fakeFrame({}), isEditable: true, editFlags: EDIT_FLAGS_ALL });
  await h.waitForPopups(1);
  assert.deepEqual(itemLines(h.popped[0].template), [
    'role:cut',
    'role:copy',
    'role:paste',
    '---',
    'role:selectAll',
    '---',
    'Inspect Element',
  ]);
  h.fire({ frame: fakeFrame({}, { neverAnswers: true }), editFlags: {} });
  await h.waitForPopups(2);
  assert.deepEqual(itemLines(h.popped[1].template), ['Inspect Element']);
});

test('registerContextMenuFor: a right-click overtaken by a newer one while its rows were read pops nothing', async () => {
  const h = makeHarness();
  const { page } = pageWithBridge(PAGE_SECTIONS);
  const overtaken = fakeFrame(page, { answersLater: true });
  h.fire({ frame: overtaken, editFlags: {} });
  h.fire({ frame: fakeFrame(page), editFlags: {} });
  await h.waitForPopups(1);
  overtaken.answer();
  await delay(20);
  assert.equal(h.popped.length, 1);
  assert.equal(itemLines(h.popped[0].template)[0], 'Rename');
});

test('registerContextMenuFor: a right-click with no items pops no menu', async () => {
  const h = makeHarness({ showsInspectElement: false });
  h.fire({ frame: fakeFrame({}), isEditable: false, selectionText: '', editFlags: {} });
  await delay(40);
  assert.equal(h.popped.length, 0);
});

test('registerContextMenuFor: a destroyed window pops nothing', async () => {
  const h = makeHarness({ winDestroyed: true });
  h.fire({ frame: fakeFrame({}), isEditable: true, editFlags: EDIT_FLAGS_ALL });
  await delay(40);
  assert.equal(h.popped.length, 0);
});
