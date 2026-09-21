// Unit tests for the AppImage's self-registered desktop entry.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');

const { desktopEntryPaths, renderDesktopEntry } = require('../../electron/linux-desktop-entry');

test('paths default to ~/.local/share and honor XDG_DATA_HOME', () => {
  const defaults = desktopEntryPaths({ homeDir: '/home/alice', xdgDataHome: undefined });
  assert.equal(defaults.desktopEntryPath, '/home/alice/.local/share/applications/minds.desktop');
  assert.equal(defaults.iconPath, '/home/alice/.local/share/icons/hicolor/512x512/apps/minds.png');

  const custom = desktopEntryPaths({ homeDir: '/home/alice', xdgDataHome: '/data/xdg' });
  assert.equal(custom.applicationsDir, '/data/xdg/applications');
  assert.equal(custom.desktopEntryPath, '/data/xdg/applications/minds.desktop');

  // An empty XDG_DATA_HOME is unset, per the spec.
  const blank = desktopEntryPaths({ homeDir: '/home/alice', xdgDataHome: '  ' });
  assert.equal(blank.applicationsDir, '/home/alice/.local/share/applications');
});

test('the entry launches the AppImage with the URL argument and claims the scheme', () => {
  const entry = renderDesktopEntry({ appImagePath: '/home/alice/Apps/Mind 0.5.3.AppImage', productName: 'Mind' });
  const lines = entry.split('\n');
  assert.equal(lines[0], '[Desktop Entry]');
  assert.ok(lines.includes('Exec="/home/alice/Apps/Mind 0.5.3.AppImage" %U'));
  // Both carry the app's productName: the menu shows it, and the window
  // manager matches the entry to the window through WM_CLASS, which Electron
  // sets to the same name.
  assert.ok(lines.includes('Name=Mind'));
  assert.ok(lines.includes('StartupWMClass=Mind'));
  assert.ok(lines.includes('MimeType=x-scheme-handler/minds;'));
  assert.ok(lines.includes('Icon=minds'));
  assert.ok(entry.endsWith('\n'));
});

test('a relative or unquotable path, or a missing name, is refused rather than written', () => {
  const render = (appImagePath) => renderDesktopEntry({ appImagePath, productName: 'Mind' });
  assert.throws(() => render('Mind.AppImage'), /must be absolute/);
  assert.throws(() => render('/tmp/a"b.AppImage'), /cannot be quoted/);
  assert.throws(() => render('/tmp/a\nb.AppImage'), /cannot be quoted/);
  // The rest of what the spec reserves inside a quoted Exec argument, plus
  // the field-code introducer.
  for (const reserved of ['\\', '$', '`', '%']) {
    assert.throws(() => render(`/tmp/a${reserved}b.AppImage`), /cannot be quoted/);
  }
  assert.throws(() => renderDesktopEntry({ appImagePath: '/tmp/Mind.AppImage' }), /product name/);
  assert.throws(() => renderDesktopEntry({ appImagePath: '/tmp/Mind.AppImage', productName: ' ' }), /product name/);
});
