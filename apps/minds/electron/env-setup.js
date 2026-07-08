const { spawn } = require('child_process');
const fs = require('fs');
const paths = require('./paths');

/**
 * Run `uv sync` using the bundled uv binary and the bundled pyproject.toml.
 * Reports progress to the renderer process via the provided callback.
 *
 * In dev mode, the monorepo workspace venv is used directly, so env setup
 * is skipped entirely.
 *
 * Returns a promise that resolves on success or rejects with error details.
 */
function runEnvSetup(onProgress) {
  if (paths.isDev()) {
    onProgress('Dev mode -- using monorepo environment');
    return Promise.resolve();
  }

  return new Promise((resolve, reject) => {
    const uvPath = paths.getUvPath();
    const pyprojectDir = paths.getPyprojectDir();
    const venvDir = paths.getVenvDir();
    const uvCacheDir = paths.getUvCacheDir();
    const uvPythonDir = paths.getUvPythonDir();
    const logDir = paths.getLogDir();

    // Ensure log directory exists
    fs.mkdirSync(logDir, { recursive: true });

    onProgress('Setting up environment...');

    // Workspace packages that we ship as freshly-built wheels with each
    // release. Their PEP 440 version (e.g. minds-0.1.0) stays the same
    // across releases, so without an explicit reinstall hint uv considers
    // them already-installed and skips updating them on upgrade -- the
    // user keeps running the OLD code in ~/.minds/.venv even after the
    // signed .app bundle has been replaced. Forcing --reinstall-package
    // for each one makes `uv sync` re-extract our wheels every launch,
    // while PyPI deps stay cached. This list is mirrored in
    // scripts/build.js, electron/pyproject/pyproject.toml, and
    // scripts/build_test.py; the drift guard
    // test_workspace_package_lists_are_consistent in build_test.py fails
    // if any of them disagree, so update all four together.
    const WORKSPACE_PACKAGES = [
      'minds',
      'imbue-mngr',
      'imbue-mngr-aws',
      'imbue-mngr-claude',
      'imbue-mngr-forward',
      'imbue-mngr-imbue-cloud',
      'imbue-mngr-latchkey',
      'imbue-mngr-lima',
      'imbue-mngr-modal',
      'imbue-mngr-ovh',
      'imbue-mngr-vps',
      'imbue-common',
      'concurrency-group',
      'resource-guards',
      'modal-proxy',
      'overlay',
    ];

    const args = [
      'sync',
      '--project', pyprojectDir,
      // --active makes uv honor VIRTUAL_ENV instead of `<project-dir>/.venv`
      // (which is inside the signed .app bundle and read-only on macOS).
      '--active',
      '--python-preference', 'only-managed',
      ...WORKSPACE_PACKAGES.flatMap((p) => ['--reinstall-package', p]),
    ];

    const env = {
      ...process.env,
      VIRTUAL_ENV: venvDir,
      UV_CACHE_DIR: uvCacheDir,
      UV_PYTHON_INSTALL_DIR: uvPythonDir,
    };

    const child = spawn(uvPath, args, {
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    let processOutput = '';

    child.stderr.on('data', (data) => {
      const text = data.toString();
      processOutput += text;

      // Parse progress from uv output
      const lines = text.split('\n');
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;

        if (trimmed.includes('Installing')) {
          onProgress('Installing packages...');
        } else if (trimmed.includes('Resolved')) {
          onProgress('Resolved dependencies...');
        } else if (trimmed.includes('Downloading')) {
          onProgress('Downloading packages...');
        } else if (trimmed.includes('Python')) {
          onProgress('Setting up Python...');
        }
      }
    });

    child.stdout.on('data', (data) => {
      processOutput += data.toString();
    });

    child.on('error', (err) => {
      reject(new Error(`Failed to start uv: ${err.message}\n\n${processOutput}`));
    });

    child.on('exit', (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(
          `uv sync failed with exit code ${code}\n\n${processOutput}`
        ));
      }
    });
  });
}

module.exports = { runEnvSetup };
