/**
 * Build script for Minds desktop app.
 *
 * Downloads platform-specific uv and git binaries, bundles workspace Python
 * packages, and copies the standalone pyproject.toml + lockfile into the
 * resources directory for packaging.
 *
 * Binary downloads are handled by download-binaries.js (shared with the
 * todesktop:beforeInstall hook for cross-platform builds).
 */

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const RESOURCES_DIR = path.join(ROOT, 'resources');

/**
 * Workspace packages that must be bundled for the standalone app.
 * Each entry maps a package name (as it appears in [tool.uv.sources])
 * to its path relative to the monorepo root.
 *
 * This list is derived from the editable sources in electron/pyproject/uv.lock.
 */
const WORKSPACE_PACKAGES = {
  'minds':             'apps/minds',
  'imbue-mngr':        'libs/mngr',
  'imbue-mngr-claude': 'libs/mngr_claude',
  'imbue-mngr-modal':  'libs/mngr_modal',
  'imbue-common':      'libs/imbue_common',
  'concurrency-group': 'libs/concurrency_group',
  'resource-guards':   'libs/resource_guards',
  'modal-proxy':       'libs/modal_proxy',
};

/**
 * Recursively copy a directory, skipping patterns that are not needed at
 * runtime (tests, caches, build artifacts, .git).
 */
function copyDirFiltered(src, dest) {
  const SKIP = new Set([
    '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
    'node_modules', '.git', '.test_output', '.venv', 'resources',
  ]);
  const SKIP_SUFFIXES = ['_test.py', '.pyc'];

  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (SKIP.has(entry.name)) continue;
    if (entry.name.startsWith('test_') && entry.name.endsWith('.py')) continue;
    if (SKIP_SUFFIXES.some(s => entry.name.endsWith(s))) continue;

    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      copyDirFiltered(srcPath, destPath);
    } else {
      fs.copyFileSync(srcPath, destPath);
    }
  }
}

/**
 * Strip [tool.uv.sources] from a bundled package's pyproject.toml.
 *
 * Each workspace package has its own [tool.uv.sources] with entries like
 * `imbue-common = { workspace = true }`. These are only valid inside the
 * monorepo workspace. In the bundled layout, the top-level pyproject.toml
 * provides all source overrides, so per-package sources must be removed.
 */
function stripUvSourcesFromPackagePyproject(pyprojectPath) {
  if (!fs.existsSync(pyprojectPath)) return;
  let content = fs.readFileSync(pyprojectPath, 'utf-8');
  const original = content;
  content = content.replace(/\[tool\.uv\.sources\][^\[]*/, '').trimEnd() + '\n';
  if (content !== original) {
    fs.writeFileSync(pyprojectPath, content);
  }
}

function copyWorkspacePackages() {
  const MONOREPO_ROOT = path.resolve(ROOT, '../..');
  const packagesDir = path.join(RESOURCES_DIR, 'packages');
  fs.mkdirSync(packagesDir, { recursive: true });

  for (const [name, relPath] of Object.entries(WORKSPACE_PACKAGES)) {
    const srcDir = path.join(MONOREPO_ROOT, relPath);
    const destName = path.basename(relPath);
    const destDir = path.join(packagesDir, destName);
    if (!fs.existsSync(srcDir)) {
      throw new Error(`Workspace package source not found: ${srcDir}`);
    }
    copyDirFiltered(srcDir, destDir);

    // Strip [tool.uv.sources] from the bundled copy so uv doesn't
    // complain about missing workspace members
    stripUvSourcesFromPackagePyproject(path.join(destDir, 'pyproject.toml'));

    console.log(`Bundled workspace package: ${name} (${relPath} -> packages/${destName})`);
  }
}

function copyPyproject() {
  const srcDir = path.join(ROOT, 'electron', 'pyproject');
  const destDir = path.join(RESOURCES_DIR, 'pyproject');
  fs.mkdirSync(destDir, { recursive: true });

  // Copy pyproject.toml, rewriting [tool.uv.sources] to point to
  // the bundled packages in ../packages/ instead of monorepo paths.
  const pyprojectSrc = path.join(srcDir, 'pyproject.toml');
  if (fs.existsSync(pyprojectSrc)) {
    let content = fs.readFileSync(pyprojectSrc, 'utf-8');

    // Build the new [tool.uv.sources] section pointing to bundled packages
    const sourceLines = ['[tool.uv.sources]'];
    for (const [name, relPath] of Object.entries(WORKSPACE_PACKAGES)) {
      const destName = path.basename(relPath);
      sourceLines.push(`${name} = { path = "../packages/${destName}", editable = true }`);
    }
    const newSources = sourceLines.join('\n') + '\n';

    // Replace existing [tool.uv.sources] section, or append if not present
    if (content.match(/\[tool\.uv\.sources\]/)) {
      content = content.replace(/\[tool\.uv\.sources\][^\[]*/, newSources);
    } else {
      content = content.trimEnd() + '\n\n' + newSources;
    }

    fs.writeFileSync(path.join(destDir, 'pyproject.toml'), content);
    console.log(`Copied pyproject.toml to ${destDir} (rewrote sources to bundled packages)`);
  } else {
    console.warn(`Warning: ${pyprojectSrc} not found`);
  }

  // Copy lockfile, rewriting editable paths to point to bundled packages.
  const lockSrc = path.join(srcDir, 'uv.lock');
  if (fs.existsSync(lockSrc)) {
    let lockContent = fs.readFileSync(lockSrc, 'utf-8');

    // Rewrite editable source paths: ../../../../libs/mngr -> ../packages/mngr
    // and ../../ -> ../packages/minds (the app itself)
    // Sort by path length (longest first) to avoid prefix collisions
    // (e.g. "../../" is a prefix of "../../../../libs/mngr")
    const replacements = Object.entries(WORKSPACE_PACKAGES).map(([, relPath]) => {
      const destName = path.basename(relPath);
      const monorepoRelative = path.relative(
        path.join(ROOT, 'electron', 'pyproject'),
        path.join(ROOT, '../..', relPath),
      ).replaceAll('\\', '/');
      return { from: monorepoRelative, to: `../packages/${destName}` };
    });
    replacements.sort((a, b) => b.from.length - a.from.length);

    for (const { from, to } of replacements) {
      lockContent = lockContent.split(from).join(to);
    }

    fs.writeFileSync(path.join(destDir, 'uv.lock'), lockContent);
    console.log(`Copied uv.lock to ${destDir} (rewrote paths to bundled packages)`);
  } else {
    console.warn(`Warning: ${lockSrc} not found`);
  }
}

async function main() {
  console.log('Building Minds desktop app...\n');

  // Clean resources directory
  if (fs.existsSync(RESOURCES_DIR)) {
    fs.rmSync(RESOURCES_DIR, { recursive: true });
  }
  fs.mkdirSync(RESOURCES_DIR, { recursive: true });

  // Download platform-specific binaries (uv, git) for the current platform.
  // On ToDesktop build servers, the beforeInstall hook re-runs this for the
  // target platform, replacing these with the correct binaries.
  console.log('Downloading platform-specific binaries...');
  execSync(`node "${path.join(__dirname, 'download-binaries.js')}" "${RESOURCES_DIR}"`, {
    stdio: 'inherit',
  });

  copyWorkspacePackages();
  copyPyproject();

  console.log('\nBuild complete!');
  console.log(`Resources directory: ${RESOURCES_DIR}`);
}

main().catch((err) => {
  console.error('Build failed:', err);
  process.exit(1);
});
