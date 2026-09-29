// Unit tests for the macOS bundle identity the ToDesktop config declares.
//
// todesktop.js is plain node (no Electron), so it is readable directly.

const test = require('node:test');
const assert = require('node:assert/strict');

const config = require('../../todesktop');
const pkg = require('../../package.json');

// Electron builds the child-process path as `<CFBundleName> Helper.app`
// (electron_main_delegate_mac.mm), and the builder names the helper bundles
// from productName. CFBundleName is unset unless extendInfo overrides it, in
// which case the builder defaults it to productName.
function effectiveBundleName() {
  return config.mac.extendInfo.CFBundleName ?? pkg.productName;
}

test('the name Electron resolves helpers through matches the helpers the builder writes', () => {
  // A mismatch of even one space makes every launch exit with
  // "Unable to find helper app" -- the whole app, not a subsystem.
  assert.equal(effectiveBundleName(), pkg.productName);
});

test('the human-readable name is carried by CFBundleDisplayName', () => {
  // Where the display name belongs, since nothing resolves paths through it.
  assert.equal(config.mac.extendInfo.CFBundleDisplayName, 'Imbue Studio');
});

test('the name the Linux executable takes carries no space', () => {
  // electron-builder names that binary after package.json's `name`, and
  // ToDesktop's schema has no executableName to override it with. The .deb's
  // AppArmor profile is named after the binary, and linux-sandbox.js compares
  // it to the first whitespace-delimited token of /proc/self/attr/current, so a
  // spaced name could never match and the app would run unsandboxed instead of
  // failing visibly.
  assert.ok(!/\s/.test(pkg.name), `name ${JSON.stringify(pkg.name)} contains whitespace`);
  assert.ok(!/^minds?$/i.test(pkg.name), `name ${JSON.stringify(pkg.name)} is a retired product name`);
});
