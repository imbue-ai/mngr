const { spawn } = require('child_process');
const net = require('net');
const fs = require('fs');
const path = require('path');
const paths = require('./paths');

let backendProcess = null;

/**
 * Find an available port by briefly binding to port 0.
 */
function findAvailablePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
    server.on('error', reject);
  });
}

/**
 * Wait until a TCP connection to host:port succeeds, up to maxAttempts.
 */
function waitForPort(host, port, maxAttempts = 50, intervalMs = 200) {
  return new Promise((resolve, reject) => {
    let attempts = 0;
    function tryConnect() {
      attempts++;
      const socket = new net.Socket();
      socket.setTimeout(500);

      function retryOrFail() {
        socket.destroy();
        if (attempts >= maxAttempts) {
          reject(new Error(`Server not ready after ${maxAttempts} attempts on port ${port}`));
        } else {
          setTimeout(tryConnect, intervalMs);
        }
      }

      socket.once('connect', () => {
        socket.destroy();
        resolve();
      });
      socket.once('error', retryOrFail);
      socket.once('timeout', retryOrFail);
      socket.connect(port, host);
    }
    tryConnect();
  });
}

/**
 * Spawn the Python backend and wait for the login URL.
 *
 * Returns a promise that resolves with { loginUrl, port } when the backend
 * is ready, or rejects if the process exits before emitting the URL.
 */
function startBackend(onProgress) {
  return new Promise((resolve, reject) => {
    let isResolved = false;

    findAvailablePort().then((port) => {
      const uvPath = paths.getUvPath();
      const uvBinDir = paths.getUvBinDir();
      const gitBinDir = paths.getGitBinDir();
      const uvCacheDir = paths.getUvCacheDir();
      const uvPythonDir = paths.getUvPythonDir();
      const pyprojectDir = paths.getPyprojectDir();
      const logDir = paths.getLogDir();

      // Ensure log directory exists
      fs.mkdirSync(logDir, { recursive: true });

      const logFile = path.join(logDir, 'minds.log');
      const logStream = fs.createWriteStream(logFile, { flags: 'a' });

      onProgress('Starting Minds...');

      const args = [
        'run', '--project', pyprojectDir,
        'mind', '--log-format', 'jsonl',
        'forward',
        '--host', '127.0.0.1',
        '--port', String(port),
      ];

      const env = {
        ...process.env,
        PATH: `${uvBinDir}:${gitBinDir}:${process.env.PATH}`,
        UV_CACHE_DIR: uvCacheDir,
        UV_PYTHON_INSTALL_DIR: uvPythonDir,
      };
      // Remove VIRTUAL_ENV to avoid uv warnings about path mismatches
      delete env.VIRTUAL_ENV;

      const child = spawn(uvPath, args, {
        env,
        cwd: pyprojectDir,
        stdio: ['ignore', 'pipe', 'pipe'],
      });

      backendProcess = child;

      let stderrBuffer = '';

      child.stderr.on('data', (data) => {
        const text = data.toString();
        logStream.write(text);
        stderrBuffer += text;

        // Parse JSONL lines from stderr
        const lines = stderrBuffer.split('\n');
        // Keep the last incomplete line in the buffer
        stderrBuffer = lines.pop() || '';

        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const event = JSON.parse(line);
            // Check for login URL in the extra field
            if (event.extra && event.extra.login_url) {
              if (!isResolved) {
                isResolved = true;
                // Wait for the server to actually start listening before resolving
                waitForPort('127.0.0.1', port).then(() => {
                  resolve({ loginUrl: event.extra.login_url, port });
                }).catch((err) => {
                  reject(new Error(`Backend emitted login URL but server never became ready: ${err.message}`));
                });
              }
            }
          } catch {
            // Not valid JSON -- just log it
          }
        }
      });

      child.stdout.on('data', (data) => {
        logStream.write(data.toString());
      });

      child.on('error', (err) => {
        logStream.end();
        if (!isResolved) {
          isResolved = true;
          reject(new Error(`Failed to start backend: ${err.message}`));
        }
      });

      child.on('exit', (code) => {
        backendProcess = null;
        logStream.end();
        if (!isResolved) {
          isResolved = true;
          reject(new Error(
            `Backend exited with code ${code} before emitting login URL`
          ));
        }
      });
    }).catch(reject);
  });
}

/**
 * Shut down the backend process gracefully (SIGTERM, then SIGKILL after 5s).
 */
function shutdown() {
  return new Promise((resolve) => {
    if (!backendProcess) {
      resolve();
      return;
    }

    const child = backendProcess;
    let isExited = false;

    child.on('exit', () => {
      isExited = true;
      backendProcess = null;
      resolve();
    });

    child.kill('SIGTERM');

    setTimeout(() => {
      if (!isExited) {
        try {
          child.kill('SIGKILL');
        } catch {
          // Process may have already exited
        }
      }
      // Resolve after SIGKILL attempt regardless
      setTimeout(() => {
        if (!isExited) {
          backendProcess = null;
          resolve();
        }
      }, 500);
    }, 5000);
  });
}

/**
 * Get the backend process (for monitoring).
 */
function getBackendProcess() {
  return backendProcess;
}

module.exports = { startBackend, shutdown, getBackendProcess };
