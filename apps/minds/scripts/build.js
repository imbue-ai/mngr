/**
 * Build script for Minds desktop app.
 *
 * Downloads platform-specific uv and git binaries, copies the standalone
 * pyproject.toml + lockfile into the resources directory for packaging.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const https = require('https');
const http = require('http');
const { execSync, execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const RESOURCES_DIR = path.join(ROOT, 'resources');

const UV_VERSION = '0.7.12';

function getPlatformArch() {
  const platform = process.platform;
  const arch = process.arch;

  if (platform === 'darwin' && arch === 'arm64') return { platform: 'darwin', arch: 'aarch64' };
  if (platform === 'darwin' && arch === 'x64') return { platform: 'darwin', arch: 'x86_64' };
  if (platform === 'linux' && arch === 'x64') return { platform: 'linux', arch: 'x86_64' };
  throw new Error(`Unsupported platform/arch: ${platform}/${arch}`);
}

function getUvDownloadUrl({ platform, arch }) {
  const target = platform === 'darwin'
    ? `uv-${arch}-apple-darwin`
    : `uv-${arch}-unknown-linux-gnu`;
  return `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${target}.tar.gz`;
}

function download(url) {
  return new Promise((resolve, reject) => {
    const client = url.startsWith('https') ? https : http;
    client.get(url, { headers: { 'User-Agent': 'minds-build' } }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume(); // Drain the redirect response to free the connection
        download(res.headers.location).then(resolve).catch(reject);
        return;
      }
      if (res.statusCode !== 200) {
        res.resume(); // Drain the error response to free the connection
        reject(new Error(`HTTP ${res.statusCode} for ${url}`));
        return;
      }
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => resolve(Buffer.concat(chunks)));
      res.on('error', reject);
    }).on('error', reject);
  });
}

async function downloadUv({ platform, arch }) {
  const uvDir = path.join(RESOURCES_DIR, 'uv');
  fs.mkdirSync(uvDir, { recursive: true });

  const url = getUvDownloadUrl({ platform, arch });
  console.log(`Downloading uv from ${url}...`);

  const tarball = await download(url);
  const tarPath = path.join(uvDir, 'uv.tar.gz');
  fs.writeFileSync(tarPath, tarball);

  // Extract the tarball
  execSync(`tar xzf "${tarPath}" -C "${uvDir}" --strip-components=1`, { stdio: 'inherit' });
  fs.unlinkSync(tarPath);

  // Verify the binary exists
  const uvBinary = path.join(uvDir, 'uv');
  if (!fs.existsSync(uvBinary)) {
    throw new Error(`uv binary not found at ${uvBinary} after extraction`);
  }
  fs.chmodSync(uvBinary, 0o755);
  console.log(`uv binary installed at ${uvBinary}`);
}

/**
 * Read and parse a JSON file.
 */
function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf-8'));
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

    // Copy the flat, self-contained node_modules tree into resources/.
    // dereference: true so any symlinks npm may have created (e.g. .bin/
    // entries) become real files -- the packaged app is read-only and may
    // be installed under a different root than the build host.
    fs.mkdirSync(destDir, { recursive: true });
    fs.cpSync(stagingNodeModules, destNodeModules, {
      recursive: true,
      dereference: true,
    });
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
    'exec env ELECTRON_RUN_AS_NODE=1 "$MINDS_ELECTRON_EXEC_PATH" "$CLI_JS" "$@"\n';
  fs.writeFileSync(shimPath, shimContent);
  fs.chmodSync(shimPath, 0o755);

  // Smoke-test the bundle by running the CLI under the build host's Node.
  // This catches missing dependencies (ERR_MODULE_NOT_FOUND) at build time
  // rather than at user launch. We invoke cli.js directly rather than going
  // through the shim because the shim requires Electron; plain Node works
  // because cli.js only uses standard Node APIs and its bundled deps.
  const bundledCli = path.join(destNodeModules, 'latchkey', cliRelative);
  console.log(`Smoke-testing bundled latchkey: ${bundledCli} --help`);
  execFileSync(process.execPath, [bundledCli, '--help'], { stdio: 'inherit' });

  console.log(
    `latchkey@${latchkey.pkg.version} bundled at ${destNodeModules} ` +
    `(shim: ${shimPath})`
  );
}

async function downloadGit() {
  const gitDir = path.join(RESOURCES_DIR, 'git');
  const binDir = path.join(gitDir, 'bin');
  fs.mkdirSync(binDir, { recursive: true });

  // Copy the system git binary into the resources directory.
  const systemGit = execSync('which git', { encoding: 'utf-8' }).trim();
  if (!systemGit) {
    throw new Error('git not found on system -- install git first');
  }

  const destGit = path.join(binDir, 'git');
  fs.copyFileSync(systemGit, destGit);
  fs.chmodSync(destGit, 0o755);
  console.log(`git binary copied to ${destGit}`);
}

function copyPyproject() {
  const srcDir = path.join(ROOT, 'electron', 'pyproject');
  const destDir = path.join(RESOURCES_DIR, 'pyproject');
  fs.mkdirSync(destDir, { recursive: true });

  // Copy pyproject.toml, stripping any [tool.uv.sources] section that
  // contains local editable paths (only valid in the monorepo layout)
  const pyprojectSrc = path.join(srcDir, 'pyproject.toml');
  if (fs.existsSync(pyprojectSrc)) {
    let content = fs.readFileSync(pyprojectSrc, 'utf-8');
    content = content.replace(/\[tool\.uv\.sources\][^\[]*/, '').trimEnd() + '\n';
    fs.writeFileSync(path.join(destDir, 'pyproject.toml'), content);
    console.log(`Copied pyproject.toml to ${destDir} (stripped local sources)`);
  } else {
    console.warn(`Warning: ${pyprojectSrc} not found`);
  }

  // Copy lockfile as-is
  const lockSrc = path.join(srcDir, 'uv.lock');
  if (fs.existsSync(lockSrc)) {
    fs.copyFileSync(lockSrc, path.join(destDir, 'uv.lock'));
    console.log(`Copied uv.lock to ${destDir}`);
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

  const { platform, arch } = getPlatformArch();
  console.log(`Platform: ${platform}, Architecture: ${arch}\n`);

  // Download binaries and copy pyproject in parallel
  await Promise.all([
    downloadUv({ platform, arch }),
    downloadGit(),
  ]);

  bundleLatchkey();
  copyPyproject();

  console.log('\nBuild complete!');
  console.log(`Resources directory: ${RESOURCES_DIR}`);
}

main().catch((err) => {
  console.error('Build failed:', err);
  process.exit(1);
});
