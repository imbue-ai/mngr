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
const { downloadGit, downloadUv, downloadRestic, download } = require('./download-binaries.js');

const ROOT = path.resolve(__dirname, '..');
const RESOURCES_DIR = path.join(ROOT, 'resources');

// Pinned at 2.0.3 to avoid the gvisor-tap-vsock TCP forwarder regression
// introduced in lima 2.1.0. Lima 2.1.x's usernet forwarder (the path used
// when the guest's systemd < 256, which includes Debian 12 / systemd 252,
// the mngr_lima default image) wedges fresh ssh connections post-VM-READY:
// TCP-accepted on the host then CLOSE_WAIT, no data flow to the in-VM
// sshd, no git-receive-pack ever spawns -- mngr create hangs forever at
// "Transferring git repository...". Root cause is the inetaf/tcpproxy
// "half-close dance" leaking goroutines in io.Copy; lima a2b52885
// (gvisor-tap-vsock 0.8.7 -> 0.8.8) is the regression boundary.
// Tracked upstream as lima-vm/lima#4558 + #5042, no fix in flight yet.
// Unaffected by mngr_lima's PINNED_DOCKER_APT_VERSION: the bug sits in
// lima's host-side TCP forwarder, below the guest docker daemon.
const LIMA_VERSION = '2.0.3';

const MONOREPO_ROOT = path.resolve(ROOT, '../..');

/**
 * Workspace packages bundled into the standalone app. Each entry maps the
 * package name (as it appears in `dependencies` / `[tool.uv.sources]`) to its
 * path inside the monorepo.
 *
 * The packaged app only needs the transitive runtime closure of what minds
 * imports; other workspace members (e.g. mngr_vps, mngr_kanpan) are
 * not included.
 *
 * This list is mirrored in electron/env-setup.js, electron/pyproject/
 * pyproject.toml, and scripts/build_test.py. The drift guard
 * `test_workspace_package_lists_are_consistent` in build_test.py fails if any
 * of them disagree, so update all four together.
 */
const WORKSPACE_PACKAGES = {
  'minds':                  'apps/minds',
  'imbue-mngr':             'libs/mngr',
  'imbue-mngr-aws':         'libs/mngr_aws',
  'imbue-mngr-claude':      'libs/mngr_claude',
  'imbue-mngr-forward':     'libs/mngr_forward',
  'imbue-mngr-imbue-cloud': 'libs/mngr_imbue_cloud',
  'imbue-mngr-latchkey':    'libs/mngr_latchkey',
  'imbue-mngr-lima':        'libs/mngr_lima',
  'imbue-mngr-modal':       'libs/mngr_modal',
  'imbue-mngr-ovh':         'libs/mngr_ovh',
  'imbue-mngr-vps':         'libs/mngr_vps',
  'imbue-common':           'libs/imbue_common',
  'concurrency-group':      'libs/concurrency_group',
  'resource-guards':        'libs/resource_guards',
  'modal-proxy':            'libs/modal_proxy',
  'overlay':                'libs/overlay',
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
/**
 * Compile the desktop client's Tailwind v4 stylesheet
 * (static/app.css -> static/app.min.css) before the minds wheel is built.
 *
 * app.min.css is gitignored and force-included into the wheel via
 * `[tool.hatch.build] artifacts` in apps/minds/pyproject.toml, so it MUST
 * exist on disk before buildWorkspaceWheels() runs -- otherwise the packaged
 * app ships unstyled. Delegates to the pinned @tailwindcss/cli via the
 * `build:css` pnpm script (also exposed as `just minds-css`).
 */
function buildCss() {
  console.log('Compiling Tailwind CSS (static/app.css -> static/app.min.css)...');
  execSync('pnpm run build:css', { cwd: ROOT, stdio: 'inherit' });
}

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
 * Bundle the latchkey npm CLI (plus all its runtime dependencies) into
 * ``resources/latchkey/``.
 *
 * Context:
 *   apps/minds is managed by pnpm, which installs each package into its own
 *   ``node_modules/.pnpm/<pkg>@<ver>/node_modules/<pkg>/`` directory and
 *   wires up sibling symlinks for deps. The store cannot be shipped inside
 *   asar: copied symlinks break on relocate/sign and the packager can't
 *   traverse the store.
 *
 *   ``pnpm deploy`` is the purpose-built tool for producing a
 *   self-contained, copyable package directory. With ``inject-workspace-
 *   packages=true`` (modern, non-legacy mode) it uses the workspace
 *   lockfile to pin every transitive, so the bundled
 *   ``playwright`` / ``playwright-core`` match exactly what
 *   ``pnpm-lock.yaml`` resolved. We deploy the ``minds`` workspace
 *   package itself with ``--prod`` to exclude minds' devDeps (the e2e
 *   playwright + electron) and copy the resulting ``node_modules`` into
 *   ``resources/latchkey/``.
 *
 *   The ``--config`` flags steer the deploy:
 *     - ``node-linker=hoisted`` produces a flat top-level layout (real
 *       directories, not symlinks into a virtual store) so asar can
 *       traverse it. Only ``node_modules/.bin/*`` retain internal
 *       symlinks; dereferenceSymlinksInPlace() materializes those.
 *     - ``ignore-scripts=true`` prevents playwright's postinstall from
 *       downloading ~500MB of browser binaries into the bundle --
 *       latchkey fetches Chromium lazily at runtime via ``ensure-browser``.
 *     - ``inject-workspace-packages=true`` is the gate pnpm 10 requires
 *       to use the modern (lockfile-respecting) deploy implementation.
 *       It's a no-op for our case because minds has no workspace deps.
 *
 *   Cross-platform native prebuilds (``@napi-rs/keyring-*``, playwright
 *   ``fsevents``) come in via the workspace-level ``supportedArchitectures``
 *   block in ``pnpm-workspace.yaml`` -- pnpm materializes every prebuild
 *   variant listed there, regardless of the build host's OS/arch/libc.
 *
 *   ``@todesktop/runtime`` and its own transitives (electron-updater,
 *   builder-util-runtime, ...) ride along because they're also prod deps
 *   of minds. They sit next to latchkey in ``resources/latchkey/``
 *   without colliding; the small extra weight is acceptable in exchange
 *   for not having to compute a latchkey-only transitive closure.
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

  const stagingParent = fs.mkdtempSync(path.join(os.tmpdir(), 'minds-latchkey-'));
  try {
    const stagingDir = path.join(stagingParent, 'staging');

    console.log('Running pnpm deploy to stage latchkey + minds prod deps...');
    execFileSync(
      'pnpm',
      [
        '--filter', 'minds',
        'deploy',
        '--prod',
        '--config.node-linker=hoisted',
        '--config.ignore-scripts=true',
        '--config.inject-workspace-packages=true',
        stagingDir,
      ],
      { cwd: ROOT, stdio: 'inherit' }
    );

    const stagingNodeModules = path.join(stagingDir, 'node_modules');
    if (!fs.existsSync(path.join(stagingNodeModules, 'latchkey', 'package.json'))) {
      throw new Error(
        `pnpm deploy did not produce latchkey under ${stagingNodeModules}`
      );
    }

    // Copy the flat, self-contained node_modules tree into resources/.
    // Top-level package directories are real (hoisted linker); only
    // node_modules/.bin/* are symlinks (internal, relative to siblings).
    // dereferenceSymlinksInPlace() materializes those as real files so
    // macOS code-signing doesn't ENOENT on dangling targets after the
    // scratch dir is removed.
    fs.mkdirSync(destDir, { recursive: true });
    fs.cpSync(stagingNodeModules, destNodeModules, {
      recursive: true,
      dereference: true,
    });
    dereferenceSymlinksInPlace(destNodeModules);
  } finally {
    fs.rmSync(stagingParent, { recursive: true, force: true });
  }

  const latchkeyPkg = readJson(path.join(destNodeModules, 'latchkey', 'package.json'));
  const cliRelative =
    typeof latchkeyPkg.bin === 'string'
      ? latchkeyPkg.bin
      : latchkeyPkg.bin && latchkeyPkg.bin.latchkey;
  if (!cliRelative) {
    throw new Error(`latchkey@${latchkeyPkg.version} is missing a "bin" entry`);
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
    `latchkey@${latchkeyPkg.version} bundled at ${destNodeModules} ` +
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

/**
 * Write the current git SHA into electron/build-info.json so the runtime
 * can surface it in the About panel. Falls back to "unknown" if the
 * working tree has no .git (e.g. building from a tarball).
 */
function bakeBuildInfo() {
  let gitSha;
  try {
    gitSha = execSync('git rev-parse HEAD', { cwd: MONOREPO_ROOT }).toString().trim();
  } catch (err) {
    console.warn(`Could not resolve git SHA (${err.message}); falling back to "unknown".`);
    gitSha = 'unknown';
  }
  const buildInfoPath = path.join(ROOT, 'electron', 'build-info.json');
  fs.writeFileSync(buildInfoPath, JSON.stringify({ gitSha }) + '\n');
  console.log(`Bundled gitSha=${gitSha} -> ${buildInfoPath}`);
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

  // The source-tree _bundled/ above ends up inside app.asar, which the
  // Python backend subprocess cannot read. paths.js getBundledConfigDir()
  // resolves the packaged-mode bundle under the pyproject resources dir
  // (extraResources copies resources/ -> Resources/), so stage a second
  // copy there on the real filesystem where `minds run --config-file`
  // can reach it.
  const packagedBundledDir = path.join(
    RESOURCES_DIR,
    'pyproject',
    'imbue',
    'minds',
    'config',
    'envs',
    '_bundled'
  );
  fs.mkdirSync(packagedBundledDir, { recursive: true });
  fs.copyFileSync(resolvedConfig, path.join(packagedBundledDir, 'client.toml'));
  fs.writeFileSync(path.join(packagedBundledDir, 'root_name'), rootNameBundle + '\n');
  console.log(`Staged bundled config for packaged runtime at ${packagedBundledDir}`);
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
  let content = fs.readFileSync(pyprojectSrc, 'utf-8');

  const sourceLines = ['[tool.uv.sources]'];
  for (const [name, whlFile] of Object.entries(wheelByPackage)) {
    sourceLines.push(`${name} = { path = "../wheels/${whlFile}" }`);
  }
  const newSources = sourceLines.join('\n') + '\n';

  // Anchor at start-of-line so the literal "[tool.uv.sources]" substring
  // inside the file-level docstring (a comment that names the section) is
  // not mistaken for the section header itself.
  const sectionRe = /^\[tool\.uv\.sources\][^\[]*/m;
  if (sectionRe.test(content)) {
    content = content.replace(sectionRe, newSources);
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
    downloadRestic(RESOURCES_DIR, { platform, arch }),
  ]);

  buildCss();
  bundleLatchkey();
  const wheelByPackage = buildWorkspaceWheels();
  stageRuntimePyproject(wheelByPackage);
  bundleClientConfig();
  bakeBuildInfo();

  console.log('\nBuild complete!');
  console.log(`Resources directory: ${RESOURCES_DIR}`);
}

main().catch((err) => {
  console.error('Build failed:', err);
  process.exit(1);
});
