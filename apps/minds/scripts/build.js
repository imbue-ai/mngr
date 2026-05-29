/**
 * Build script for Minds desktop app.
 *
 * Downloads platform-specific uv, git, and Lima binaries, copies the
 * standalone pyproject.toml + lockfile into the resources directory for
 * packaging.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execSync, execFileSync } = require('child_process');
const { downloadGit, downloadUv, download } = require('./download-binaries.js');

const ROOT = path.resolve(__dirname, '..');
const RESOURCES_DIR = path.join(ROOT, 'resources');

const LIMA_VERSION = '2.1.1';

function getPlatformArch() {
  const platform = process.platform;
  const arch = process.arch;

  if (platform === 'darwin' && arch === 'arm64') return { platform: 'darwin', arch: 'aarch64' };
  if (platform === 'darwin' && arch === 'x64') return { platform: 'darwin', arch: 'x86_64' };
  if (platform === 'linux' && arch === 'x64') return { platform: 'linux', arch: 'x86_64' };
  throw new Error(`Unsupported platform/arch: ${platform}/${arch}`);
}

/**
 * Download a gzipped tarball to ``destDir`` and extract it in place with
 * ``--strip-components=1``, then verify and chmod the named binary.
 *
 * Used for binaries that ship as a single self-contained tarball rooted one
 * level deep (e.g. Lima). ``label`` is used only for log lines and error
 * messages; ``archiveName`` is the on-disk filename for the downloaded
 * tarball (deleted after extraction); ``binaryPath`` is the absolute path
 * the caller expects the extracted binary to live at.
 */
async function downloadAndExtractTarball({ destDir, url, archiveName, binaryPath, label }) {
  fs.mkdirSync(destDir, { recursive: true });
  console.log(`Downloading ${label} from ${url}...`);

  const tarball = await download(url);
  const tarPath = path.join(destDir, archiveName);
  fs.writeFileSync(tarPath, tarball);

  execSync(`tar xzf "${tarPath}" -C "${destDir}" --strip-components=1`, { stdio: 'inherit' });
  fs.unlinkSync(tarPath);

  if (!fs.existsSync(binaryPath)) {
    throw new Error(`${label} binary not found at ${binaryPath} after extraction`);
  }
  fs.chmodSync(binaryPath, 0o755);
  console.log(`${label} binary installed at ${binaryPath}`);
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

function getLimaDownloadUrl({ platform, arch }) {
  // Lima release tarballs are named lima-<version>-<OsLabel>-<archLabel>.tar.gz.
  // The OS label is title-cased (Darwin/Linux). The arch label differs by OS:
  // Darwin uses arm64, Linux uses aarch64; both use x86_64 for Intel.
  const osLabel = platform === 'darwin' ? 'Darwin' : 'Linux';
  let archLabel;
  if (arch === 'x86_64') {
    archLabel = 'x86_64';
  } else if (arch === 'aarch64') {
    archLabel = platform === 'darwin' ? 'arm64' : 'aarch64';
  } else {
    throw new Error(`Unsupported Lima arch: ${arch}`);
  }
  return `https://github.com/lima-vm/lima/releases/download/v${LIMA_VERSION}/lima-${LIMA_VERSION}-${osLabel}-${archLabel}.tar.gz`;
}

async function downloadLima({ platform, arch }) {
  // We keep the full extracted layout (bin/ + share/ + libexec/) because
  // limactl resolves its templates and guest-agent payloads via paths
  // relative to its own executable.
  const limaDir = path.join(RESOURCES_DIR, 'lima');
  await downloadAndExtractTarball({
    destDir: limaDir,
    url: getLimaDownloadUrl({ platform, arch }),
    archiveName: 'lima.tar.gz',
    binaryPath: path.join(limaDir, 'bin', 'limactl'),
    label: 'Lima',
  });

  // Strip Darwin guest-agents. Each one is a gzipped arm64/x86_64 Mach-O,
  // and Apple's notarytool unzips it and rejects the inner binary because
  // we never code-signed it (no Developer ID, no hardened runtime, no
  // secure timestamp). We run Linux VMs only via Lima, so Darwin guest-
  // agents are unreachable code and safe to delete.
  const limaShareDir = path.join(limaDir, 'share', 'lima');
  if (fs.existsSync(limaShareDir)) {
    for (const entry of fs.readdirSync(limaShareDir)) {
      if (entry.startsWith('lima-guestagent.Darwin-') && entry.endsWith('.gz')) {
        const full = path.join(limaShareDir, entry);
        fs.rmSync(full);
        console.log(`Stripped Darwin guest-agent (unsignable inside .gz): ${full}`);
      }
    }
  }
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

/**
 * Bake an explicit client.toml (and the matching MINDS_ROOT_NAME) into
 * _bundled/ so the shipped desktop client passes --config-file
 * explicitly at startup and writes its on-disk state to the right env
 * root.
 *
 * Both env vars are required for any non-dev packaged build:
 *
 *   - MINDS_CLIENT_CONFIG_BUNDLE: absolute or relative path to the
 *     client.toml the build should embed. For staging / production
 *     builds, this is the in-repo
 *     apps/minds/imbue/minds/config/envs/<tier>/client.toml. For beta
 *     builds, it can point anywhere -- the build does not interpret
 *     the file, just copies it verbatim into _bundled/client.toml.
 *
 *   - MINDS_ROOT_NAME_BUNDLE: the MINDS_ROOT_NAME the runtime should
 *     export before launching `minds run`. Production builds use
 *     "minds" (so on-disk state lands in ~/.minds/); a staging build
 *     uses "minds-staging" (so state lands in ~/.minds-staging/ and
 *     never collides with an installed prod build). Must match the
 *     minds(-<env-name>)? shape enforced by the runtime bootstrap.
 *
 * When both are unset, leaves _bundled/ empty -- this is the
 * `uv run minds run` / dev-mode case where the user is expected to
 * activate an env in their shell (`minds env activate <name>`) before
 * invoking the backend. The packaged Electron startup refuses to run
 * without a bundled config if it was built without these vars set,
 * which surfaces the missing build-time config loudly instead of
 * silently shipping a dev-only artifact.
 *
 * Refuses if exactly one of the two is set -- both knobs travel
 * together (the config path identifies WHICH env's URLs ship; the root
 * name identifies WHERE the runtime should write its state). A
 * mismatched build is almost certainly an oversight.
 */
function bundleClientConfig() {
  const configBundle = process.env.MINDS_CLIENT_CONFIG_BUNDLE;
  const rootNameBundle = process.env.MINDS_ROOT_NAME_BUNDLE;
  const bundledDir = path.join(
    ROOT,
    'imbue',
    'minds',
    'config',
    'envs',
    '_bundled'
  );
  const bundledClient = path.join(bundledDir, 'client.toml');
  const bundledRootNameFile = path.join(bundledDir, 'root_name');

  // Start from a clean slate so a previous build's artifact doesn't leak
  // into this one (a developer flipping the bundle vars off should
  // produce a no-bundled-config build, not yesterday's production URL).
  for (const stale of [bundledClient, bundledRootNameFile]) {
    if (fs.existsSync(stale)) {
      fs.rmSync(stale);
    }
  }
  fs.mkdirSync(bundledDir, { recursive: true });

  if (!configBundle && !rootNameBundle) {
    console.log(
      'MINDS_CLIENT_CONFIG_BUNDLE / MINDS_ROOT_NAME_BUNDLE both unset; ' +
        'leaving _bundled/ empty. Packaged runtime will refuse to start ' +
        'without an activated env in the user\'s shell -- this is the ' +
        'dev-build path.'
    );
    return;
  }
  if (!configBundle || !rootNameBundle) {
    throw new Error(
      'MINDS_CLIENT_CONFIG_BUNDLE and MINDS_ROOT_NAME_BUNDLE must both be ' +
        'set (or both unset); got ' +
        `MINDS_CLIENT_CONFIG_BUNDLE=${JSON.stringify(configBundle || null)}, ` +
        `MINDS_ROOT_NAME_BUNDLE=${JSON.stringify(rootNameBundle || null)}.`
    );
  }

  // The runtime regex on MINDS_ROOT_NAME is `minds(-<env-name>)?` with
  // env-name = `[a-z0-9][a-z0-9_-]{0,38}[a-z0-9]`. Validate here so a
  // bad build env fails loudly at build time instead of producing a
  // bundle that the runtime then ignores at startup.
  if (!/^minds(-[a-z0-9][a-z0-9_-]{0,38}[a-z0-9])?$/.test(rootNameBundle)) {
    throw new Error(
      `MINDS_ROOT_NAME_BUNDLE=${JSON.stringify(rootNameBundle)} does not match ` +
        '`minds(-<env-name>)?` (where env-name is the same regex DevEnvName enforces).'
    );
  }

  const resolvedConfig = path.isAbsolute(configBundle)
    ? configBundle
    : path.resolve(ROOT, configBundle);
  if (!fs.existsSync(resolvedConfig)) {
    throw new Error(
      `MINDS_CLIENT_CONFIG_BUNDLE='${configBundle}' (resolved to '${resolvedConfig}') ` +
        'does not exist.'
    );
  }
  fs.copyFileSync(resolvedConfig, bundledClient);
  fs.writeFileSync(bundledRootNameFile, rootNameBundle + '\n');
  console.log(`Bundled ${resolvedConfig} -> ${bundledClient}`);
  console.log(`Bundled MINDS_ROOT_NAME=${rootNameBundle} -> ${bundledRootNameFile}`);
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
    downloadUv(RESOURCES_DIR, { platform, arch }),
    downloadLima({ platform, arch }),
    downloadGit(RESOURCES_DIR, { platform }),
  ]);

  bundleLatchkey();
  copyPyproject();
  bundleClientConfig();

  console.log('\nBuild complete!');
  console.log(`Resources directory: ${RESOURCES_DIR}`);
}

main().catch((err) => {
  console.error('Build failed:', err);
  process.exit(1);
});
