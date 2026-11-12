'use strict';

// The desktop client's right-click (context) menu: one native menu for every
// frame of a window, the chrome page's and the workspace's alike. Kept free of
// any `electron` import so it can be unit-tested under plain node (see
// ../test/unit/context-menu.test.js); main.js injects Electron's Menu,
// clipboard, and menu symbols.
//
// A workspace page running the template's element menu leaves the right-click
// to this menu (it was told `drawsContextMenus` through the embed contract's
// embedder-capabilities) and keeps its own rows: the page's, the link row only
// it can follow, and the reference rows. The menu reads them from the
// right-clicked frame through the page's PAGE_MENU_GLOBAL bridge, places them
// among the native items, and runs a picked one back in that frame.

// The bridge's name, sections, and bounds, which the workspace template's
// element menu (workspace_ui's context_menu.ts) is tested against.
const PAGE_MENU_BRIDGE = require('./page-menu-bridge.json');

const PAGE_MENU_GLOBAL = PAGE_MENU_BRIDGE.global;
const PAGE_MENU_SECTIONS = PAGE_MENU_BRIDGE.sections;
const PAGE_ROW_KINDS = new Set(PAGE_MENU_BRIDGE.row_kinds);
const PAGE_SUBMENU_ROW_KINDS = new Set(PAGE_MENU_BRIDGE.submenu_row_kinds);
const MAX_PAGE_ROWS_PER_SECTION = PAGE_MENU_BRIDGE.max_rows_per_section;
const MAX_PAGE_ROWS_PER_SUBMENU = PAGE_MENU_BRIDGE.max_rows_per_submenu;
const MAX_PAGE_LABEL_LENGTH = PAGE_MENU_BRIDGE.max_label_length;
const SF_SYMBOL_PATTERN = new RegExp(PAGE_MENU_BRIDGE.symbol_pattern);
/** How long the menu waits for a frame's rows; a frame that has not answered by then gets the native items alone. */
const PAGE_MENU_READ_TIMEOUT_MS = 300;
const MAX_SPELLING_SUGGESTIONS = 5;
/** How much of the selection the Look Up row quotes. */
const MAX_QUOTED_SELECTION_LENGTH = 24;

/**
 * At most `count` of the text's characters, counted by code point so an emoji at the cut is not split into a lone
 * surrogate. Reads no further than the cut, since the text may be a page's label or a selection of any length.
 */
function firstCodePoints(text, count) {
  let end = 0;
  let taken = 0;
  for (const codePoint of text) {
    if (taken === count) break;
    end += codePoint.length;
    taken += 1;
  }
  return text.slice(0, end);
}

/** The label, tooltip, and symbol every row but a divider carries; null without a label. */
function readLabelledFields(raw) {
  if (typeof raw.label !== 'string' || raw.label.trim() === '') return null;
  return {
    label: firstCodePoints(raw.label, MAX_PAGE_LABEL_LENGTH),
    tooltip: typeof raw.tooltip === 'string' ? firstCodePoints(raw.tooltip, MAX_PAGE_LABEL_LENGTH) : null,
    symbol: typeof raw.symbol === 'string' && SF_SYMBOL_PATTERN.test(raw.symbol) ? raw.symbol : null,
  };
}

function readPageMenuItems(rawItems, maxRows, kinds) {
  const items = Array.isArray(rawItems) ? rawItems : [];
  return withoutStrayDividers(
    items
      .slice(0, maxRows)
      .map((raw) => readPageMenuItem(raw, kinds))
      .filter((item) => item !== null),
  );
}

function readPageMenuItem(raw, kinds) {
  if (raw === null || typeof raw !== 'object' || !kinds.has(raw.kind)) return null;
  if (raw.kind === 'divider') return { kind: 'divider' };
  const fields = readLabelledFields(raw);
  if (fields === null) return null;
  if (raw.kind === 'submenu') {
    const items = readPageMenuItems(raw.items, MAX_PAGE_ROWS_PER_SUBMENU, PAGE_SUBMENU_ROW_KINDS);
    return items.length === 0 ? null : { kind: 'submenu', ...fields, items };
  }
  if (typeof raw.key !== 'string' || raw.key === '') return null;
  if (raw.kind === 'check') return { kind: 'check', key: raw.key, ...fields, isChecked: raw.isChecked === true };
  return { kind: 'action', key: raw.key, ...fields, isDisabled: raw.isDisabled === true };
}

/** The items with no divider at either end and none twice in a row, since the menu puts its own between sections. */
function withoutStrayDividers(items) {
  const kept = [];
  for (const item of items) {
    if (item.kind === 'divider' && (kept.length === 0 || kept[kept.length - 1].kind === 'divider')) continue;
    kept.push(item);
  }
  if (kept.length > 0 && kept[kept.length - 1].kind === 'divider') kept.pop();
  return kept;
}

/**
 * The rows a frame's page bridge answered, checked: the page is a workspace
 * app's (an agent may have built it), so only well-formed rows within the
 * bounds survive. Null when the answer is not a sections object at all.
 *
 * @param {unknown} raw  What the page's `sections()` returned.
 * @returns {{page: Array<object>, link: Array<object>, reference: Array<object>} | null}
 */
function readPageMenuSections(raw) {
  if (raw === null || typeof raw !== 'object') return null;
  return Object.fromEntries(
    PAGE_MENU_SECTIONS.map((name) => [name, readPageMenuItems(raw[name], MAX_PAGE_ROWS_PER_SECTION, PAGE_ROW_KINDS)]),
  );
}

/** The groups in order with one separator between each pair of non-empty ones. */
function joinGroups(groups) {
  const template = [];
  for (const group of groups) {
    if (group.length === 0) continue;
    if (template.length > 0) template.push({ type: 'separator' });
    template.push(...group);
  }
  return template;
}

/** The text on one line (a menu item's title shows a newline as a line break), cut short, in curly quotes. */
function quoted(text) {
  const line = text.replace(/\s+/g, ' ');
  const cut = firstCodePoints(line, MAX_QUOTED_SELECTION_LENGTH);
  return `“${cut === line ? line : `${cut.trimEnd()}…`}”`;
}

/**
 * The context-menu template for a right-click, from the Chromium
 * `context-menu` event params and the rows the right-clicked page kept for it
 * (null for a frame with none); empty when the right-click has nothing to
 * offer. An item that acts calls into `actions`, which registerContextMenuFor
 * binds to the window, its web contents, and the frame; a role item is run by
 * Electron itself.
 *
 * @param {object} params  Electron 'context-menu' event params.
 * @param {{pageSections: object | null, platform: string, showsInspectElement: boolean, actions: object}} context
 * @returns {Array<object>}  A template for Menu.buildFromTemplate.
 */
function buildContextMenuTemplate(params, { pageSections, platform, showsInspectElement, actions }) {
  const p = params || {};
  const isMac = platform === 'darwin';
  const selection = typeof p.selectionText === 'string' ? p.selectionText.trim() : '';
  const sections = pageSections || readPageMenuSections({});

  const withSymbol = (item, symbol) => {
    if (!isMac || symbol === null) return item;
    const icon = actions.menuSymbol(symbol);
    return icon === null ? item : { ...item, icon };
  };
  const pageItem = (row) => {
    if (row.kind === 'divider') return { type: 'separator' };
    let item;
    if (row.kind === 'submenu') item = { label: row.label, submenu: row.items.map(pageItem) };
    else if (row.kind === 'check') {
      item = { label: row.label, type: 'checkbox', checked: row.isChecked, click: () => actions.selectPageRow(row.key) };
    } else item = { label: row.label, enabled: !row.isDisabled, click: () => actions.selectPageRow(row.key) };
    if (row.tooltip !== null) item.toolTip = row.tooltip;
    return withSymbol(item, row.symbol);
  };
  const pageItems = (rows) => rows.map(pageItem);

  const lookUp = isMac && selection !== '' ? [{ label: `Look Up ${quoted(selection)}`, click: () => actions.lookUp() }] : [];
  const inspect = showsInspectElement ? [{ label: 'Inspect Element', click: () => actions.inspectAt(p.x, p.y) }] : [];

  return joinGroups([
    spellingItems(p, actions),
    pageItems(sections.page),
    lookUp,
    editItems(p, selection),
    [...pageItems(sections.link), ...linkAndImageItems(p, actions)],
    isMac ? macSystemItems(p, selection, withSymbol) : [],
    pageItems(sections.reference),
    inspect,
  ]);
}

function linkURLOf(p) {
  return typeof p.linkURL === 'string' ? p.linkURL : '';
}

function imageURLOf(p) {
  return p.mediaType === 'image' && typeof p.srcURL === 'string' ? p.srcURL : '';
}

function spellingItems(p, actions) {
  if (typeof p.misspelledWord !== 'string' || p.misspelledWord === '') return [];
  const word = p.misspelledWord;
  const suggestions = (p.dictionarySuggestions || []).slice(0, MAX_SPELLING_SUGGESTIONS);
  const items = suggestions.map((suggestion) => ({ label: suggestion, click: () => actions.replaceMisspelling(suggestion) }));
  if (suggestions.length === 0) items.push({ label: 'No Guesses Found', enabled: false });
  items.push({ label: 'Add to Dictionary', click: () => actions.addToDictionary(word) });
  return items;
}

function editItems(p, selection) {
  const editFlags = p.editFlags || {};
  if (p.isEditable) {
    return [
      { role: 'cut', enabled: !!editFlags.canCut },
      { role: 'copy', enabled: !!editFlags.canCopy },
      { role: 'paste', enabled: !!editFlags.canPaste },
      { type: 'separator' },
      { role: 'selectAll', enabled: !!editFlags.canSelectAll },
    ];
  }
  if (!!editFlags.canCopy || selection !== '') return [{ role: 'copy', enabled: true }];
  return [];
}

function linkAndImageItems(p, actions) {
  const items = [];
  const linkURL = linkURLOf(p);
  if (linkURL !== '') items.push({ label: 'Copy Link', click: () => actions.copyText(linkURL) });
  const imageURL = imageURLOf(p);
  if (imageURL !== '') {
    items.push(
      { label: 'Copy Image', click: () => actions.copyImageAt(p.x, p.y) },
      { label: 'Copy Image Address', click: () => actions.copyText(imageURL) },
    );
  }
  return items;
}

/** Share (the link, else the selection, else the image), and Speech and Services for a selection. */
function macSystemItems(p, selection, withSymbol) {
  const items = [];
  const linkURL = linkURLOf(p);
  const imageURL = imageURLOf(p);
  let sharingItem = null;
  if (linkURL !== '') sharingItem = { urls: [linkURL] };
  else if (selection !== '') sharingItem = { texts: [selection] };
  else if (imageURL !== '') sharingItem = { urls: [imageURL] };
  if (sharingItem !== null) items.push(withSymbol({ role: 'shareMenu', sharingItem }, 'square.and.arrow.up'));
  if (selection !== '') {
    items.push(
      withSymbol({ label: 'Speech', submenu: [{ role: 'startSpeaking' }, { role: 'stopSpeaking' }] }, 'text.bubble'),
      { role: 'services' },
    );
  }
  return items;
}

/**
 * The rows the right-clicked frame's page kept for the menu, or null: a frame
 * with no bridge, one gone, one whose answer is malformed, or one that has not
 * answered within `timeoutMs`.
 *
 * @param {{executeJavaScript: Function, isDestroyed: () => boolean} | null} frame  The params' WebFrameMain.
 * @param {number} timeoutMs
 * @returns {Promise<object | null>}
 */
function readFrameMenuSections(frame, timeoutMs) {
  if (!frame || frame.isDestroyed()) return Promise.resolve(null);
  let timer = null;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => {
      console.warn(`[context-menu] the page menu rows did not arrive within ${timeoutMs} ms; showing the native items alone`);
      resolve(null);
    }, timeoutMs);
  });
  const read = frame
    .executeJavaScript(`globalThis.${PAGE_MENU_GLOBAL}?.sections?.() ?? null`)
    .then(readPageMenuSections, (err) => {
      console.warn('[context-menu] could not read the page menu rows:', err && err.message);
      return null;
    });
  return Promise.race([read, timeout]).finally(() => clearTimeout(timer));
}

/** Run a picked page row in its frame, with the window raised so the page can take focus for it. */
function selectPageRow(win, frame, key) {
  if (win.isDestroyed() || !frame || frame.isDestroyed()) return;
  win.focus();
  frame
    .executeJavaScript(`globalThis.${PAGE_MENU_GLOBAL}?.select?.(${JSON.stringify(key)})`, true)
    .catch((err) => console.warn('[context-menu] could not run the page menu row:', err && err.message));
}

/**
 * Wire the right-click (context) menu onto a window's web contents: on each
 * `context-menu` event, read the right-clicked frame's rows, build the template
 * with {@link buildContextMenuTemplate}, and pop a native menu over the window
 * when it has any items. A right-click overtaken by a newer one while its rows
 * were being read pops nothing.
 *
 * @param {{isDestroyed: () => boolean, focus: () => void}} win  The BrowserWindow the menu pops over.
 * @param {object} wc  `win`'s webContents.
 * @param {{Menu: object, clipboard: object, createMenuSymbol: (name: string) => object, platform: string, showsInspectElement: boolean}} deps
 */
function registerContextMenuFor(win, wc, deps) {
  let latestRightClick = 0;
  wc.on('context-menu', (_event, params) => {
    if (win.isDestroyed() || wc.isDestroyed()) return;
    latestRightClick += 1;
    const rightClick = latestRightClick;
    const frame = (params && params.frame) || null;
    const actions = {
      replaceMisspelling: (word) => wc.replaceMisspelling(word),
      addToDictionary: (word) => wc.session.addWordToSpellCheckerDictionary(word),
      lookUp: () => wc.showDefinitionForSelection(),
      copyText: (text) => deps.clipboard.writeText(text),
      copyImageAt: (x, y) => wc.copyImageAt(x, y),
      inspectAt: (x, y) => wc.inspectElement(x, y),
      menuSymbol: (name) => {
        const image = deps.createMenuSymbol(name);
        return image.isEmpty() ? null : image;
      },
      selectPageRow: (key) => selectPageRow(win, frame, key),
    };
    readFrameMenuSections(frame, PAGE_MENU_READ_TIMEOUT_MS).then((pageSections) => {
      if (rightClick !== latestRightClick || win.isDestroyed() || wc.isDestroyed()) return;
      const template = buildContextMenuTemplate(params, {
        pageSections,
        platform: deps.platform,
        showsInspectElement: deps.showsInspectElement,
        actions,
      });
      if (template.length === 0) return;
      deps.Menu.buildFromTemplate(template).popup({ window: win });
    });
  });
}

module.exports = { buildContextMenuTemplate, readPageMenuSections, registerContextMenuFor };
