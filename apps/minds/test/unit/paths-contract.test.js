// Unit tests for the paths.js export surface the rest of the shell consumes.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// paths.js imports `electron`, and so does every module that consumes it, so
// none of them can be required under plain node. Their references are checked
// statically instead: a renamed export otherwise reaches a packaged build as a
// `TypeError: paths.<name> is not a function` on whichever code path touches it.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ELECTRON_DIR = path.join(__dirname, '..', '..', 'electron');
const PATHS_MODULE = 'paths';

/** The names in a module's `module.exports = { ... };` object literal. */
function exportedNames(source) {
  const block = source.match(/module\.exports = \{([^}]*)\};/);
  assert.ok(block, 'paths.js has no `module.exports = { ... };` block to read');
  return new Set(
    block[1]
      .split(',')
      .map((entry) => entry.trim().split(':')[0].trim())
      .filter((name) => name.length > 0)
  );
}

/** Every shell module that binds paths.js to a local `const <name> = require('./paths')`. */
function consumersOfPaths() {
  const consumers = [];
  for (const fileName of fs.readdirSync(ELECTRON_DIR)) {
    if (!fileName.endsWith('.js')) {
      continue;
    }
    const source = fs.readFileSync(path.join(ELECTRON_DIR, fileName), 'utf8');
    const binding = source.match(/const (\w+) = require\('\.\/paths'\);/);
    if (binding) {
      consumers.push({ fileName, source, binding: binding[1] });
    }
  }
  return consumers;
}

test('paths.js is consumed by name across the shell', () => {
  // Guards the guard: if the require shape ever changes, the reference check
  // below would pass by finding nothing to check.
  const consumers = consumersOfPaths().map((consumer) => consumer.fileName);
  assert.ok(consumers.includes('updater.js'), `updater.js is not among ${consumers.join(', ')}`);
  assert.ok(consumers.length >= 5, `only ${consumers.length} modules bind paths.js`);
});

test('every paths.<name> reference in the shell is exported by paths.js', () => {
  const exported = exportedNames(fs.readFileSync(path.join(ELECTRON_DIR, `${PATHS_MODULE}.js`), 'utf8'));
  const dangling = [];
  for (const { fileName, source, binding } of consumersOfPaths()) {
    for (const [, name] of source.matchAll(new RegExp(`\\b${binding}\\.(\\w+)`, 'g'))) {
      if (!exported.has(name)) {
        dangling.push(`${fileName}: ${binding}.${name}`);
      }
    }
  }
  assert.deepEqual(dangling, [], `paths.js exports nothing by these names:\n  ${dangling.join('\n  ')}`);
});
