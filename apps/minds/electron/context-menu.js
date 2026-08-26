'use strict';

// Pure logic for the desktop client's right-click (context) menu over editable
// fields and selected text. Kept free of any `electron` import so it can be
// unit-tested under plain node (see ../test/unit/context-menu.test.js); main.js
// turns the descriptor list this returns into a real Menu and pops it up.
//
// The chat input -- and every other editable field -- lives inside web content
// main loads: the Mithril SPA, and for the workspace surface a sandboxed
// iframe. Building the menu here from Chromium's own edit flags, rather than
// per-framework, is what makes Cut/Copy/Paste/Select All work in the chat box
// no matter which frame renders it, and Copy work on any selected text (e.g.
// the chat transcript) even where nothing is editable.

/**
 * The context-menu item descriptors for a right-click, from the Chromium
 * `context-menu` event params. Returns an empty array when no menu should
 * show -- a right-click that is neither on an editable field nor on a text
 * selection -- so main.js pops nothing there (an empty or all-disabled menu is
 * just noise, and matches the app's prior no-menu behavior in that spot).
 *
 * Editable target: Cut / Copy / Paste, a separator, then Select All -- each
 * greyed per the matching Chromium edit flag, so Paste greys with an empty
 * clipboard and Cut/Copy grey with no selection. Non-editable target with a
 * selection: Copy alone (Cut / Paste / Select All are meaningless on read-only
 * text). The descriptors use standard menu-item roles, so the actual clipboard
 * actions come from Electron's Menu.buildFromTemplate in main.js.
 *
 * @param {object} params                  Electron 'context-menu' event params.
 * @param {boolean} [params.isEditable]     The right-clicked element is editable.
 * @param {string}  [params.selectionText]  The currently selected text, if any.
 * @param {object}  [params.editFlags]      Chromium's edit-command availability.
 * @param {boolean} [params.editFlags.canCut]
 * @param {boolean} [params.editFlags.canCopy]
 * @param {boolean} [params.editFlags.canPaste]
 * @param {boolean} [params.editFlags.canSelectAll]
 * @returns {Array<{role?: string, type?: string, enabled?: boolean}>}
 */
function buildContextMenuTemplate(params) {
  const p = params || {};
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

  const hasSelection =
    !!editFlags.canCopy ||
    (typeof p.selectionText === 'string' && p.selectionText.trim().length > 0);
  if (hasSelection) {
    return [{ role: 'copy', enabled: true }];
  }

  return [];
}

/**
 * Wire the right-click (context) menu onto a window's web contents: on each
 * `context-menu` event, build the item list with {@link buildContextMenuTemplate}
 * and, when it is non-empty, pop a native menu over the window.
 *
 * `Menu` is injected rather than imported so this module stays free of any
 * `electron` import and remains unit-testable under plain node -- main.js passes
 * electron's real `Menu` (see createBundle); the unit test passes a fake that
 * records `buildFromTemplate` / `popup`. Handling this at the Electron level
 * covers the chat input no matter which frame -- the SPA or the sandboxed
 * workspace iframe -- renders it.
 *
 * @param {{isDestroyed: () => boolean}} win  The BrowserWindow the menu pops over.
 * @param {{on: Function, isDestroyed: () => boolean}} wc  `win`'s webContents.
 * @param {{buildFromTemplate: (template: Array) => {popup: (opts: object) => void}}} Menu  Electron's Menu.
 */
function registerContextMenuFor(win, wc, Menu) {
  wc.on('context-menu', (_event, params) => {
    if (win.isDestroyed() || wc.isDestroyed()) return;
    const template = buildContextMenuTemplate(params);
    if (template.length === 0) return;
    Menu.buildFromTemplate(template).popup({ window: win });
  });
}

module.exports = { buildContextMenuTemplate, registerContextMenuFor };
