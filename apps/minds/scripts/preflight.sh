#!/usr/bin/env bash
# Locally reproduce what ToDesktop's CI does between upload and packaging,
# so we catch dep / hoisting / pnpm-policy failures BEFORE burning a 5-minute
# server-side build cycle.
#
# What ToDesktop does (per inspection of their build logs):
#   1. Download our source zip and unpack.
#   2. Run our `todesktop:beforeInstall` script (./scripts/download-binaries.js).
#   3. postProcessApplicationSource: rewrites package.json to strip
#      `@todesktop/cli` from devDependencies. (Other fields are also touched
#      but the cli strip is the only behaviour we've observed.)
#   4. Run `npx pnpm@latest install --prod=false --no-frozen-lockfile`.
#   5. Run electron-builder under the hood -- packs node_modules into
#      app.asar, downloads Electron prebuilt, code-signs, etc.
#
# Steps 1-4 are exactly what this script reproduces. Step 5 (electron-builder
# packaging) we skip because it adds 5+ minutes and the failure modes there
# (signing, notarization) are not local-reproducible anyway. We DO verify
# that any module ./electron/main.js requires is reachable at top-level
# `node_modules/` -- that's the class of bug v0.2.17 hit (electron-updater
# under .pnpm/ but not visible to the asar packager).
#
# Usage: bash scripts/preflight.sh
#   exits 0 if everything checks out (safe to push)
#   exits non-zero if anything would fail ToDesktop CI

set -euo pipefail

# Resolve script-relative paths so we work from any cwd
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRATCH_DIR="$(mktemp -d -t minds-preflight-XXXXXX)"
trap 'rm -rf "$SCRATCH_DIR"' EXIT

PNPM_VERSION="${PNPM_VERSION:-11.1.0}"

echo "==> preflight scratch dir: $SCRATCH_DIR"
echo "==> pnpm version: $PNPM_VERSION (override via PNPM_VERSION=)"

# Copy the files ToDesktop's CI receives via its source-zip step.
cp "$APP_DIR/package.json" "$SCRATCH_DIR/package.json"
cp "$APP_DIR/pnpm-lock.yaml" "$SCRATCH_DIR/pnpm-lock.yaml"
cp "$APP_DIR/pnpm-workspace.yaml" "$SCRATCH_DIR/pnpm-workspace.yaml" 2>/dev/null || true
cp "$APP_DIR/.npmrc" "$SCRATCH_DIR/.npmrc" 2>/dev/null || true
mkdir -p "$SCRATCH_DIR/electron"
cp "$APP_DIR/electron/main.js" "$SCRATCH_DIR/electron/main.js" 2>/dev/null || true

# Simulate ToDesktop's postProcessApplicationSource step: drop @todesktop/cli
# from devDependencies. Their server does this with a node script; we do it
# with python (zero extra deps).
python3 - <<PYEOF
import json, pathlib
p = pathlib.Path("$SCRATCH_DIR/package.json")
data = json.loads(p.read_text())
dev = data.get("devDependencies", {})
removed = dev.pop("@todesktop/cli", None)
if removed:
    print(f"==> stripped @todesktop/cli@{removed} from devDependencies (ToDesktop does this)")
p.write_text(json.dumps(data, indent=2) + "\n")
PYEOF

cd "$SCRATCH_DIR"

# Run ToDesktop's exact install command.
echo "==> npx pnpm@$PNPM_VERSION install --prod=false --no-frozen-lockfile"
if ! npx --yes "pnpm@$PNPM_VERSION" install --prod=false --no-frozen-lockfile > install.log 2>&1; then
    echo "!! pnpm install FAILED with exit $?"
    echo "----- last 20 lines of install.log: -----"
    tail -20 install.log
    exit 1
fi

# Even if the install command exits 0, pnpm 11 prints
# [ERR_PNPM_IGNORED_BUILDS] when a dep with a build script isn't approved
# in pnpm-workspace.yaml's allowBuilds. ToDesktop's older runners may exit
# 1 on this even when our local 11.1.0 doesn't. Catch it explicitly.
if grep -q "ERR_PNPM_IGNORED_BUILDS" install.log; then
    echo "!! pnpm reported ERR_PNPM_IGNORED_BUILDS (ToDesktop CI will exit 1)"
    grep -A2 "ERR_PNPM_IGNORED_BUILDS" install.log
    exit 1
fi

echo "==> install succeeded"

# Actually load @todesktop/runtime + the entrypoint with Node and catch any
# "Cannot find module" errors. This is the same failure shape that crashed
# the v0.2.17 packaged app at launch -- a require() inside the runtime that
# resolves to something pnpm only put under .pnpm/ and the asar packager
# didn't copy. Loading it here surfaces the same missing modules
# (electron-updater, etc.) before the build ever runs.
#
# Caveats: some requires happen inside electron-only code paths (e.g. need
# the BrowserWindow API) and others are deferred. We only catch
# MODULE_NOT_FOUND errors; other failures (electron API not available
# outside an electron process) are expected and silenced.
echo "==> scanning for require()s in electron/main.js + @todesktop/runtime"
# Key insight: Node's runtime resolution finds modules through pnpm's nested
# symlinks just fine. But ToDesktop's asar packager only copies *top-level*
# node_modules/<pkg>/ -- it doesn't follow the symlinks into .pnpm/. So a
# require() that resolves locally can STILL crash in the packaged app.
#
# This check verifies presence at top-level node_modules/<pkg>/ for every
# package referenced by a top-level require() in our code or the runtime.
# Block / line comments are stripped first to avoid false positives like
# the winston require inside @todesktop/runtime/dist/Logger.js (entire
# function is commented out as dead code).
node - <<'JSEOF'
const fs = require('fs');
const path = require('path');
const Module = require('module');

// Strip block /* ... */ and line // ... comments before searching.
function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^[ \t]*\/\/[^\n]*$/gm, '');
}

const reqRE = /require\(\s*['"]([^'"]+)['"]\s*\)/g;
const reqs = new Set();
const builtins = new Set(Module.builtinModules);

function scanFile(p) {
  try {
    const src = stripComments(fs.readFileSync(p, 'utf8'));
    let m;
    while ((m = reqRE.exec(src)) !== null) {
      const r = m[1];
      if (!r.startsWith('.') && !r.startsWith('node:') && !builtins.has(r)) reqs.add(r);
    }
  } catch {}
}

if (fs.existsSync('electron/main.js')) scanFile('electron/main.js');
// Walk @todesktop/runtime's own dist JS for the same.
function walkDistJs(modName) {
  const root = path.join('node_modules', modName);
  if (!fs.existsSync(root)) return;
  (function walk(dir) {
    for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, ent.name);
      if (ent.isDirectory() && ent.name !== 'node_modules') walk(p);
      else if (ent.isFile() && ent.name.endsWith('.js')) scanFile(p);
    }
  })(root);
}
walkDistJs('@todesktop/runtime');

// For each referenced module, check that top-level node_modules/<pkg>/
// (or the scoped @scope/name/) exists -- the packager only sees that.
const missing = [];
for (const r of [...reqs].sort()) {
  const topLevel = r.startsWith('@') ? r.split('/').slice(0, 2).join('/') : r.split('/')[0];
  const ok = fs.existsSync(path.join('node_modules', topLevel, 'package.json'));
  if (!ok) missing.push(r);
  console.log(`  ${ok ? 'ok' : 'MISSING'}: require('${r}') -> node_modules/${topLevel}/`);
}

if (missing.length > 0) {
  console.error('\n!! the following modules are NOT at top-level node_modules/:');
  for (const r of missing) console.error('     ' + r);
  console.error('\n   Node resolution may find them via pnpm symlinks, but ToDesktop\'s');
  console.error('   asar packager only copies top-level entries -- packaged app will');
  console.error('   crash with `Cannot find module \'...\'` at launch.');
  console.error('\n   Fix: set `nodeLinker: hoisted` in pnpm-workspace.yaml (recommended) so');
  console.error('   pnpm materialises every transitive at top-level node_modules/;');
  console.error('   or declare each missing module as a direct dep in package.json.');
  process.exit(1);
}
console.log('\n==> all required modules present at top-level node_modules/');
JSEOF

echo
echo "==> preflight OK -- safe to run \`pnpm exec todesktop build\`"
