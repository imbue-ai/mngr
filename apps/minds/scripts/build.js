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
const os = require('os');
const path = require('path');
const { execSync, execFileSync } = require('child_process');

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
  'minds':                  'apps/minds',
  'imbue-mngr':             'libs/mngr',
  'imbue-mngr-claude':      'libs/mngr_claude',
  'imbue-mngr-forward':     'libs/mngr_forward',
  'imbue-mngr-imbue-cloud': 'libs/mngr_imbue_cloud',
  'imbue-mngr-lima':        'libs/mngr_lima',
  'imbue-mngr-modal':       'libs/mngr_modal',
  'imbue-common':           'libs/imbue_common',
  'concurrency-group':      'libs/concurrency_group',
  'resource-guards':        'libs/resource_guards',
  'modal-proxy':            'libs/modal_proxy',
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
 * Read and parse a JSON file.
 */
function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf-8'));
}

/**
 * Recursively replace every symlink under ``root`` with a real copy of its
 * target. Needed because ``fs.cpSync({ dereference: true })`` does *not*
 * actually materialize the target's bytes into the destination for nested
 * symlinks -- it just rewrites them to absolute paths pointing back at the
 * source. After we delete the scratch staging directory those absolute
 * symlinks dangle, and electron-builder's macOS code-signing phase ENOENTs
 * on every dangling entry in ``Contents/Resources/``.
 *
 * In practice the only symlinks npm creates are under ``node_modules/.bin/``
 * (one per package with a ``bin`` entry), but we walk the whole tree for
 * generality -- if a future install produces a symlink anywhere else we'd
 * hit the same bug.
 */
function dereferenceSymlinksInPlace(root) {
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const entryPath = path.join(root, entry.name);
    if (entry.isSymbolicLink()) {
      const realPath = fs.realpathSync(entryPath);
      const realStats = fs.statSync(realPath);
      if (!realStats.isFile()) {
        throw new Error(
          `Unexpected non-file symlink target while dereferencing bundle: ` +
          `${entryPath} -> ${realPath} (${realStats.isDirectory() ? 'directory' : 'other'})`
        );
      }
      fs.rmSync(entryPath);
      fs.copyFileSync(realPath, entryPath);
      fs.chmodSync(entryPath, realStats.mode);
    } else if (entry.isDirectory()) {
      dereferenceSymlinksInPlace(entryPath);
    }
  }
}

/**
 * Resolve the on-disk package.json for a dependency as seen from a given
 * starting directory. Handles pnpm's layout (where transitive deps aren't
 * hoisted to the root node_modules) by threading the right search path.
 */
function resolveInstalledPackage(name, fromDir) {
  const packageJsonPath = require.resolve(`${name}/package.json`, {
    paths: [fromDir],
  });
  return { packageJsonPath, pkg: readJson(packageJsonPath) };
}

/**
 * Bundle the latchkey npm CLI (plus all its runtime dependencies) into
 * ``resources/latchkey/``.
 *
 * Context:
 *   apps/minds is managed by pnpm, which installs each package into its own
 *   ``node_modules/.pnpm/<pkg>@<ver>/node_modules/<pkg>/`` directory and
 *   wires up sibling symlinks for deps. Naively copying just the latchkey
 *   package directory leaves Node unable to resolve ``commander``, ``zod``,
 *   etc., because those live as siblings in the pnpm virtual store rather
 *   than as nested directories inside the package.
 *
 *   To get a self-contained, portable bundle we do a fresh, flat
 *   ``npm install`` into a scratch staging directory and copy the resulting
 *   hoisted ``node_modules/`` tree wholesale into ``resources/latchkey/``.
 *
 * Platform-fanout (native prebuilds):
 *   Some deps use ``optionalDependencies`` to ship one platform-specific
 *   prebuilt native addon per target. Specifically:
 *     - ``@napi-rs/keyring`` fans out to ``@napi-rs/keyring-<os>-<arch>[-libc]``.
 *     - ``playwright`` has an optional ``fsevents`` for macOS.
 *   npm's default installer skips any optional dep whose ``os``/``cpu``
 *   doesn't match the build host, which breaks cross-platform packaging
 *   (todesktop builds multiple targets from one host). We sidestep that by
 *   listing every such fanout dep explicitly as a top-level dependency in
 *   the staging ``package.json`` (with ``--force`` so npm doesn't refuse
 *   them). The fanout set is read from each parent package's own
 *   ``optionalDependencies``, so it tracks upstream version bumps without
 *   manual intervention.
 *
 *   ``--ignore-scripts`` prevents playwright's postinstall from downloading
 *   ~500MB of browser binaries into the staging tree -- latchkey only uses
 *   playwright lazily, and any needed browsers are fetched at runtime.
 *
 * Runtime:
 *   A small shell shim at ``resources/latchkey/bin/latchkey`` invokes the
 *   CLI under the packaged Electron binary as Node (``ELECTRON_RUN_AS_NODE=1``),
 *   so we don't need to ship a separate Node runtime. The Python backend
 *   sets ``MINDS_ELECTRON_EXEC_PATH`` in the env before spawning the shim.
 */
function bundleLatchkey() {
  const destDir = path.join(RESOURCES_DIR, 'latchkey');
  const destNodeModules = path.join(destDir, 'node_modules');
  const destBinDir = path.join(destDir, 'bin');

  // Discover versions and fanout sets from the already-pnpm-installed deps
  // under apps/minds/node_modules/. This keeps the bundled versions in lock
  // step with what dev mode and pnpm-lock.yaml pin. keyring and playwright
  // are transitive deps of latchkey, so under pnpm they aren't hoisted to
  // apps/minds/node_modules -- we resolve them starting from latchkey's own
  // install directory.
  const latchkey = resolveInstalledPackage('latchkey', ROOT);
  const latchkeyDir = path.dirname(latchkey.packageJsonPath);
  const keyring = resolveInstalledPackage('@napi-rs/keyring', latchkeyDir);
  const playwright = resolveInstalledPackage('playwright', latchkeyDir);

  const cliRelative =
    typeof latchkey.pkg.bin === 'string'
      ? latchkey.pkg.bin
      : latchkey.pkg.bin && latchkey.pkg.bin.latchkey;
  if (!cliRelative) {
    throw new Error(`latchkey@${latchkey.pkg.version} is missing a "bin" entry`);
  }

  // Union of every platform-specific optional prebuild we want to guarantee
  // is in the bundle, regardless of the build host's OS/arch/libc.
  const fanoutDeps = {
    ...(keyring.pkg.optionalDependencies || {}),
    ...(playwright.pkg.optionalDependencies || {}),
  };

  const stagingParent = fs.mkdtempSync(path.join(os.tmpdir(), 'minds-latchkey-'));
  try {
    const stagingDir = path.join(stagingParent, 'staging');
    fs.mkdirSync(stagingDir, { recursive: true });

    const stagingPackage = {
      name: 'minds-latchkey-bundle',
      version: '0.0.0',
      private: true,
      dependencies: {
        latchkey: latchkey.pkg.version,
        ...fanoutDeps,
      },
    };
    fs.writeFileSync(
      path.join(stagingDir, 'package.json'),
      JSON.stringify(stagingPackage, null, 2) + '\n'
    );

    console.log(
      `Installing latchkey@${latchkey.pkg.version} into staging with ` +
      `${Object.keys(fanoutDeps).length} platform-fanout deps...`
    );
    execFileSync(
      'npm',
      [
        'install',
        '--omit=dev',
        '--ignore-scripts',
        '--force',
        '--no-audit',
        '--no-fund',
        '--no-package-lock',
      ],
      { cwd: stagingDir, stdio: 'inherit' }
    );

    const stagingNodeModules = path.join(stagingDir, 'node_modules');
    if (!fs.existsSync(path.join(stagingNodeModules, 'latchkey', 'package.json'))) {
      throw new Error(
        `npm install did not produce latchkey under ${stagingNodeModules}`
      );
    }

    // Apply our pnpm-patch on top of the npm-installed staging tree.
    // We use `pnpm patch` for local-dev applicability (it's the only
    // patch mechanism that natively integrates with the workspace
    // install), but the bundled staging install uses `npm` here, which
    // doesn't honor pnpm's `patchedDependencies`. Without this step the
    // shipped binary carries a vanilla latchkey while the workspace
    // checkout has the patched one -- discovered the hard way when
    // 0.2.25 still crashed for end users.
    const patchFile = path.join(
      MONOREPO_ROOT, 'apps/minds/patches/latchkey@2.10.1.patch'
    );
    if (fs.existsSync(patchFile)) {
      const stagedLatchkey = path.join(stagingNodeModules, 'latchkey');
      console.log(`Applying ${path.basename(patchFile)} to staging latchkey...`);
      execFileSync(
        'patch',
        ['-p1', '--forward', '--input', patchFile],
        { cwd: stagedLatchkey, stdio: 'inherit' }
      );
    }

    // Copy the flat, self-contained node_modules tree into resources/.
    // dereference: true handles most symlinks, but nested symlinks (notably
    // node_modules/.bin/*) end up pointing back at the source tree rather
    // than being materialized as real files. dereferenceSymlinksInPlace()
    // below walks the copied tree and fixes that up, so the bundle is fully
    // self-contained and safe to package/sign/relocate.
    fs.mkdirSync(destDir, { recursive: true });
    fs.cpSync(stagingNodeModules, destNodeModules, {
      recursive: true,
      dereference: true,
    });
    dereferenceSymlinksInPlace(destNodeModules);
  } finally {
    fs.rmSync(stagingParent, { recursive: true, force: true });
  }

  // Emit the shim. It resolves the CLI relative to its own location so the
  // bundle is relocatable.
  fs.mkdirSync(destBinDir, { recursive: true });
  const shimPath = path.join(destBinDir, 'latchkey');
  const cliRelativeFromShim = path
    .join('..', 'node_modules', 'latchkey', cliRelative)
    .replace(/\\/g, '/');
  // The `--import` of a tiny data: module sets `process.defaultApp = true`
  // before latchkey's cli.js loads. This works around a commander@12 quirk:
  // commander auto-detects `process.versions.electron` and switches to
  // `from: 'electron'` arg parsing, which slices the wrong number of leading
  // entries off argv under ELECTRON_RUN_AS_NODE=1 (because
  // `process.defaultApp` is unset in that mode). The result is that
  // `latchkey <subcommand>` reports ``error: unknown command '<cli.js path>'``
  // for every real subcommand (only `--version` / `--help` work, because
  // commander scans for those before command dispatch). Forcing
  // `process.defaultApp = true` steers commander into the branch that
  // matches the real argv layout. Safe to leave in place if latchkey is
  // later fixed to pass ``{ from: 'node' }`` explicitly, since commander
  // ignores ``process.defaultApp`` once ``from`` is set.
  const shimContent =
    '#!/usr/bin/env bash\n' +
    '# Auto-generated by scripts/build.js. Runs the bundled latchkey CLI under\n' +
    '# the Electron binary (invoked as Node via ELECTRON_RUN_AS_NODE=1).\n' +
    'set -eu\n' +
    'HERE="$(cd "$(dirname "$0")" && pwd)"\n' +
    'CLI_JS="$HERE/' + cliRelativeFromShim + '"\n' +
    'if [ -z "${MINDS_ELECTRON_EXEC_PATH:-}" ]; then\n' +
    '  echo "latchkey shim: MINDS_ELECTRON_EXEC_PATH not set; cannot locate Node runtime" >&2\n' +
    '  exit 1\n' +
    'fi\n' +
    'exec env ELECTRON_RUN_AS_NODE=1 "$MINDS_ELECTRON_EXEC_PATH" \\\n' +
    '  --import \'data:text/javascript,process.defaultApp=true;\' \\\n' +
    '  "$CLI_JS" "$@"\n';
  fs.writeFileSync(shimPath, shimContent);
  fs.chmodSync(shimPath, 0o755);

  // Smoke-test the bundle by running the CLI under the build host's Node.
  // This catches missing dependencies (ERR_MODULE_NOT_FOUND) at build time
  // rather than at user launch. We invoke cli.js directly rather than going
  // through the shim because the shim requires Electron; plain Node works
  // because cli.js only uses standard Node APIs and its bundled deps.
  const bundledCli = path.join(destNodeModules, 'latchkey', cliRelative);
  console.log(`Smoke-testing bundled latchkey: ${bundledCli} --version`);
  execFileSync(process.execPath, [bundledCli, '--version'], { stdio: 'inherit' });

  console.log(
    `latchkey@${latchkey.pkg.version} bundled at ${destNodeModules} ` +
    `(shim: ${shimPath})`
  );
}

/**
 * Write `resources/pyproject/pyproject.toml` and regenerate `uv.lock`.
 *
 * Starts from `electron/pyproject/pyproject.toml` (the dev-time pyproject),
 * replaces `[tool.uv.sources]` with entries pointing at the bundled wheels,
 * and then runs `uv lock` in-place so the lockfile matches the rewritten
 * pyproject. This re-resolves PyPI deps from scratch, which is fine -- they're
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

  // Fetch Tailwind CDN bundle. Used to live in package.json's `postinstall`
  // hook, but newer pnpm (11.x, what ToDesktop's `npx pnpm@latest install`
  // pulls) treats `ERR_PNPM_IGNORED_BUILDS` for transitive deps (electron,
  // protobufjs, dtrace-provider, @firebase/util) as fatal at install time
  // when an additional postinstall is present. Pulling tailwind here keeps
  // pnpm install side-effect-free and unblocks the ToDesktop pipeline.
  console.log('Fetching Tailwind...');
  execSync(`bash "${path.join(__dirname, 'fetch_tailwind.sh')}"`, {
    cwd: ROOT, stdio: 'inherit',
  });

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

  bundleLatchkey();
  const wheelByPackage = buildWorkspaceWheels();
  stageRuntimePyproject(wheelByPackage);

  console.log('\nBuild complete!');
  console.log(`Resources directory: ${RESOURCES_DIR}`);
}

main().catch((err) => {
  console.error('Build failed:', err);
  process.exit(1);
});
