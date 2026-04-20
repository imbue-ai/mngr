/**
 * Build script for Minds desktop app.
 *
 * Downloads platform-specific uv and git binaries, builds workspace Python
 * packages as wheels, and stages a pyproject.toml + lockfile in the resources
 * directory for packaging. On the user's first launch, `uv sync` installs the
 * bundled wheels (plus their PyPI deps) into a venv.
 *
 * Binary downloads are handled by download-binaries.js (shared with the
 * todesktop:beforeInstall hook for cross-platform builds).
 *
 * Requirements:
 * - `uv` must be on PATH (used for `uv build` and `uv lock`)
 * - Node 18+ (for fs.rmSync and modern child_process semantics)
 * - Network access at build time (uv lock re-resolves PyPI deps)
 */

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const RESOURCES_DIR = path.join(ROOT, 'resources');
const MONOREPO_ROOT = path.resolve(ROOT, '../..');

/**
 * Workspace packages bundled into the standalone app. Each entry maps the
 * package name (as it appears in `dependencies` / `[tool.uv.sources]`) to its
 * path inside the monorepo.
 *
 * The packaged app only needs the transitive runtime closure of what minds
 * imports; other workspace members (e.g. mngr_vps_docker, mngr_kanpan) are
 * not included.
 */
const WORKSPACE_PACKAGES = {
  'minds':             'apps/minds',
  'imbue-mngr':        'libs/mngr',
  'imbue-mngr-claude': 'libs/mngr_claude',
  'imbue-mngr-modal': 'libs/mngr_modal',
  'imbue-common':      'libs/imbue_common',
  'concurrency-group': 'libs/concurrency_group',
  'resource-guards':   'libs/resource_guards',
  'modal-proxy':       'libs/modal_proxy',
};

/**
 * Build each workspace package as a wheel into `resources/wheels/`.
 *
 * Relies on each package's `pyproject.toml` (and hatchling's
 * `[tool.hatch.build.targets.wheel]` config) to determine what goes into the
 * wheel. In particular, the `exclude = [...]` line in each package's config
 * is what keeps tests out of the wheel.
 *
 * Returns a map of package name → wheel filename, used downstream when
 * rewriting `pyproject.toml` to reference the wheels.
 */
function buildWorkspaceWheels() {
  const wheelsDir = path.join(RESOURCES_DIR, 'wheels');
  fs.mkdirSync(wheelsDir, { recursive: true });

  const wheelByPackage = {};
  for (const name of Object.keys(WORKSPACE_PACKAGES)) {
    execSync(`uv build --package ${JSON.stringify(name)} --wheel --out-dir ${JSON.stringify(wheelsDir)}`, {
      cwd: MONOREPO_ROOT, stdio: 'inherit',
    });
    // Wheel filenames follow PEP 427: `{name}-{version}-{py}-{abi}-{platform}.whl`
    // where `name` has hyphens normalized to underscores. Since we build
    // serially and clean RESOURCES_DIR at the top of main(), exactly one
    // wheel per package should exist with the expected prefix.
    const normalized = name.replace(/-/g, '_');
    const matches = fs.readdirSync(wheelsDir).filter(
      (f) => f.endsWith('.whl') && f.startsWith(normalized + '-'),
    );
    if (matches.length !== 1) {
      throw new Error(
        `Expected exactly one wheel for ${name} (prefix "${normalized}-") in ${wheelsDir}, ` +
        `found ${matches.length}: ${JSON.stringify(matches)}`,
      );
    }
    wheelByPackage[name] = matches[0];
    console.log(`Built wheel for ${name}: ${matches[0]}`);
  }
  return wheelByPackage;
}

/**
 * Write `resources/pyproject/pyproject.toml` and regenerate `uv.lock`.
 *
 * Starts from `electron/pyproject/pyproject.toml` (the dev-time pyproject),
 * replaces `[tool.uv.sources]` with entries pointing at the bundled wheels,
 * and then runs `uv lock` in-place so the lockfile matches the rewritten
 * pyproject. This re-resolves PyPI deps from scratch, which is fine — they're
 * the same deps, just locked against the new workspace source definitions.
 */
function stageRuntimePyproject(wheelByPackage) {
  const srcDir = path.join(ROOT, 'electron', 'pyproject');
  const destDir = path.join(RESOURCES_DIR, 'pyproject');
  fs.mkdirSync(destDir, { recursive: true });

  const pyprojectSrc = path.join(srcDir, 'pyproject.toml');
  if (!fs.existsSync(pyprojectSrc)) {
    throw new Error(`Source pyproject.toml not found at ${pyprojectSrc}`);
  }
  let content = fs.readFileSync(pyprojectSrc, 'utf-8');

  const sourceLines = ['[tool.uv.sources]'];
  for (const [name, whlFile] of Object.entries(wheelByPackage)) {
    sourceLines.push(`${name} = { path = "../wheels/${whlFile}" }`);
  }
  const newSources = sourceLines.join('\n') + '\n';

  if (content.match(/\[tool\.uv\.sources\]/)) {
    content = content.replace(/\[tool\.uv\.sources\][^\[]*/, newSources);
  } else {
    content = content.trimEnd() + '\n\n' + newSources;
  }
  fs.writeFileSync(path.join(destDir, 'pyproject.toml'), content);
  console.log(`Staged pyproject.toml at ${destDir}`);

  // Regenerate the lockfile against the rewritten pyproject. This is simpler
  // and more robust than string-surgery on the dev-time uv.lock: uv emits the
  // exact right shape for wheel-path sources itself.
  execSync('uv lock', { cwd: destDir, stdio: 'inherit' });
  console.log(`Regenerated uv.lock at ${destDir}`);
}

async function main() {
  console.log('Building Minds desktop app...\n');

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

  const wheelByPackage = buildWorkspaceWheels();
  stageRuntimePyproject(wheelByPackage);

  console.log('\nBuild complete!');
  console.log(`Resources directory: ${RESOURCES_DIR}`);
}

main().catch((err) => {
  console.error('Build failed:', err);
  process.exit(1);
});
