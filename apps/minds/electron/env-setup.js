const { spawn } = require('child_process');
const fs = require('fs');
const paths = require('./paths');

/**
 * Run `uv sync` using the bundled uv binary and the bundled pyproject.toml.
 * Reports progress to the renderer process via the provided callback.
 *
 * Returns a promise that resolves on success or rejects with error details.
 */
function runEnvSetup(onProgress) {
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

    const args = [
      'sync',
      '--project', pyprojectDir,
      '--python-preference', 'only-managed',
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
