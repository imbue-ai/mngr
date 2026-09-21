'use strict';

// The freedesktop entry a running AppImage registers for itself.
//
// An AppImage is one file with no installer, so nothing registers a menu
// entry, an icon, or the minds:// scheme handler for it; the .deb ships its
// own system-wide entry from electron-builder and never reaches this.
//
// Free of any `electron` import so it is unit-testable under plain node (see
// ../test/unit/linux-desktop-entry.test.js).

const path = require('path');

const DESKTOP_ENTRY_FILENAME = 'minds.desktop';
const ICON_NAME = 'minds';
// The largest fixed size the hicolor theme indexes: a theme lookup never
// searches a directory index.theme does not list, and it stops at 512.
const ICON_PIXEL_SIZE = 512;
const ICON_SIZE = `${ICON_PIXEL_SIZE}x${ICON_PIXEL_SIZE}`;
const SCHEME_MIME_TYPE = 'x-scheme-handler/minds';

/**
 * Where the entry and icon go, per the XDG base directory spec: `$XDG_DATA_HOME`
 * when set, else `~/.local/share`.
 */
function desktopEntryPaths({ homeDir, xdgDataHome }) {
  const dataHome = xdgDataHome && xdgDataHome.trim() !== '' ? xdgDataHome : path.join(homeDir, '.local', 'share');
  const applicationsDir = path.join(dataHome, 'applications');
  return {
    applicationsDir,
    desktopEntryPath: path.join(applicationsDir, DESKTOP_ENTRY_FILENAME),
    iconPath: path.join(dataHome, 'icons', 'hicolor', ICON_SIZE, 'apps', `${ICON_NAME}.png`),
  };
}

/**
 * The desktop entry text for the AppImage at `appImagePath`, for the app
 * named `productName`.
 *
 * `productName` is the app's `productName` (Electron's `app.name`), which is
 * also the `WM_CLASS` Electron gives every window on Linux, so it serves as
 * `StartupWMClass` exactly as electron-builder's entry for the .deb uses it.
 *
 * The Exec path is quoted so a directory with a space survives, and `%U`
 * hands the minds:// URL over as an argument, which is how main.js receives
 * deeplinks on Linux (a second instance's argv, or a cold start's).
 *
 * Inside a quoted Exec argument the spec reserves `"`, backslash, `$` and
 * backtick (each needs a backslash escape on top of the file-level one) and
 * `%` starts a field code, so a path carrying any of them is refused rather
 * than written into an entry that launches the wrong file.
 */
function renderDesktopEntry({ appImagePath, productName }) {
  if (typeof productName !== 'string' || productName.trim() === '') {
    throw new Error(`The product name must be a non-empty string, got ${JSON.stringify(productName)}`);
  }
  if (typeof appImagePath !== 'string' || !path.isAbsolute(appImagePath)) {
    throw new Error(`The AppImage path must be absolute, got ${JSON.stringify(appImagePath)}`);
  }
  if (/["\\$`%\n\r]/.test(appImagePath)) {
    throw new Error(`The AppImage path cannot be quoted into a desktop entry: ${JSON.stringify(appImagePath)}`);
  }
  return [
    '[Desktop Entry]',
    'Type=Application',
    `Name=${productName}`,
    'Comment=Persistent, autonomous AI agents',
    `Exec="${appImagePath}" %U`,
    `Icon=${ICON_NAME}`,
    'Terminal=false',
    'Categories=Development;',
    `MimeType=${SCHEME_MIME_TYPE};`,
    `StartupWMClass=${productName}`,
    '',
  ].join('\n');
}

module.exports = {
  DESKTOP_ENTRY_FILENAME,
  ICON_PIXEL_SIZE,
  SCHEME_MIME_TYPE,
  desktopEntryPaths,
  renderDesktopEntry,
};
