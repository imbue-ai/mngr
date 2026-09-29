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

test('the entry launches the AppImage with the URL argument and claims both schemes', () => {
  const entry = renderDesktopEntry({
    appImagePath: '/home/alice/Apps/ImbueStudio 0.8.0.AppImage',
    productName: 'ImbueStudio',
    displayName: 'Imbue Studio',
  });
  const lines = entry.split('\n');
  assert.equal(lines[0], '[Desktop Entry]');
  assert.ok(lines.includes('Exec="/home/alice/Apps/ImbueStudio 0.8.0.AppImage" %U'));
  // The menu shows the display name; the window manager matches the entry to
  // the window through WM_CLASS, which Electron sets to the productName.
  assert.ok(lines.includes('Name=Imbue Studio'));
  assert.ok(lines.includes('StartupWMClass=ImbueStudio'));
  assert.ok(lines.includes('MimeType=x-scheme-handler/imbue-studio;x-scheme-handler/minds;'));
  assert.ok(lines.includes('Icon=minds'));
  assert.ok(entry.endsWith('\n'));
});

test('a product name containing a space survives into the entry', () => {
  // productName is "Imbue Studio", so the AppImage path and StartupWMClass both
  // carry a space: Exec must stay quoted and the WM class must not be split.
  const { productName } = require('../../package.json');
  const lines = renderDesktopEntry({
    appImagePath: '/home/alice/Apps/Imbue Studio 0.8.0.AppImage',
    productName,
    displayName: 'Imbue Studio',
  }).split('\n');
  assert.ok(lines.includes('Exec="/home/alice/Apps/Imbue Studio 0.8.0.AppImage" %U'));
  assert.ok(lines.includes(`StartupWMClass=${productName}`));
});

test('a relative or unquotable path, or a missing name, is refused rather than written', () => {
  const render = (appImagePath) =>
    renderDesktopEntry({ appImagePath, productName: 'ImbueStudio', displayName: 'Imbue Studio' });
  assert.throws(() => render('ImbueStudio.AppImage'), /must be absolute/);
  assert.throws(() => render('/tmp/a"b.AppImage'), /cannot be quoted/);
  assert.throws(() => render('/tmp/a\nb.AppImage'), /cannot be quoted/);
  // The rest of what the spec reserves inside a quoted Exec argument, plus
  // the field-code introducer.
  for (const reserved of ['\\', '$', '`', '%']) {
    assert.throws(() => render(`/tmp/a${reserved}b.AppImage`), /cannot be quoted/);
  }
  assert.throws(() => renderDesktopEntry({ appImagePath: '/tmp/ImbueStudio.AppImage' }), /product name/);
  assert.throws(
    () => renderDesktopEntry({ appImagePath: '/tmp/ImbueStudio.AppImage', productName: ' ' }),
    /product name/,
  );
  assert.throws(
    () => renderDesktopEntry({ appImagePath: '/tmp/ImbueStudio.AppImage', productName: 'ImbueStudio' }),
    /display name/,
  );
  assert.throws(
    () => renderDesktopEntry({ appImagePath: '/tmp/ImbueStudio.AppImage', productName: 'ImbueStudio', displayName: '' }),
    /display name/,
  );
});
