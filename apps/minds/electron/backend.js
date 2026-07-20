const { spawn } = require('child_process');
const net = require('net');
const fs = require('fs');
const path = require('path');
const paths = require('./paths');
const { getBuildMetadata } = require('./build-metadata');
const { createRotatingLogStream } = require('./log-rotation');

// Swallow EPIPE on the Electron main process's own stdout/stderr. When dev
// launches go through a pipe (e.g. `just minds-start | head -30`), the
// reader can exit while the backend is still alive, leaving subsequent
// writes from the dev-mode forwarder (below) to raise EPIPE asynchronously
// as an 'error' event. Without this handler the unhandled error surfaces
// as a JS alert in the Electron window. We log nothing because the
// downstream consumer is gone -- the log file is the durable record.
for (const stream of [process.stdout, process.stderr]) {
  stream.on('error', (err) => {
    if (!err || err.code !== 'EPIPE') {
      throw err;
    }
  });
}

let backendProcess = null;

// Backend stdout JSONL event fields that carry secrets and must be masked
// before the raw line is written to minds.log (which is uploaded with bug
// reports). Keyed by event type; each value lists the fields to redact.
//
//   * mngr_forward_started: the freshly-minted mngr-forward preauth cookie --
//     a reusable, session-lifetime bearer token for the local forward. The
//     backend never logs it through any other path, so masking it here removes
//     it from the logs entirely.
//
// The ``login_url`` event's one-time login code is deliberately NOT masked: it
// is single-use and consumed immediately at session start, so it is spent by
// the time any log is read (it is also common to print single-use login URLs).
// Masking it here would be a false comfort anyway -- the backend prints the
// same URL via a stderr log line and the JSONL sink, neither of which this
// stdout-only redaction touches.
const SECRET_STDOUT_EVENT_FIELDS = {
  mngr_forward_started: ['preauth_cookie'],
};

const REDACTED_PLACEHOLDER = '***';

/**
 * Return a copy of a backend stdout line safe to persist to minds.log.
 *
 * If the line is a JSON event carrying secrets (see SECRET_STDOUT_EVENT_FIELDS)
 * its secret fields are replaced with a placeholder; every other line is
 * returned unchanged. The parsed event used by the in-process handlers is
 * derived from the ORIGINAL line, so redaction never affects behavior -- only
 * what is written to disk.
 */
function redactStdoutLineForLog(line) {
  if (!line.trim()) return line;
  let event;
  try {
    event = JSON.parse(line);
  } catch {
    return line;
  }
  const secretFields = event && typeof event === 'object' ? SECRET_STDOUT_EVENT_FIELDS[event.event] : undefined;
  if (!secretFields) return line;
  const redacted = { ...event };
  for (const field of secretFields) {
    if (field in redacted) redacted[field] = REDACTED_PLACEHOLDER;
  }
  return JSON.stringify(redacted);
}

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
 * Check whether a port is free (nothing listening on it).
 *
 * Attempts a TCP connection to 127.0.0.1:port. If the connection succeeds
 * (something is listening), the port is occupied. If it fails with ECONNREFUSED,
 * the port is free.
 *
 * Returns a Promise<boolean> -- true if free, false if occupied.
 */
function isPortFree(port) {
  return new Promise((resolve) => {
    const socket = new net.Socket();
    socket.setTimeout(500);
    socket.once('connect', () => {
      socket.destroy();
      resolve(false);
    });
    socket.once('error', () => {
      socket.destroy();
      resolve(true);
    });
    socket.once('timeout', () => {
      socket.destroy();
      resolve(true);
    });
    socket.connect(port, '127.0.0.1');
  });
}

/**
 * Wait for a port to become free, polling at intervalMs up to timeoutMs.
 *
 * Returns a Promise<boolean> -- true if the port became free within the
 * timeout, false if it is still occupied.
 */
function waitForPortFree(port, timeoutMs = 6000, intervalMs = 200) {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs;
    function poll() {
      isPortFree(port).then((free) => {
        if (free) {
          resolve(true);
        } else if (Date.now() >= deadline) {
          resolve(false);
        } else {
          setTimeout(poll, intervalMs);
        }
      });
    }
    poll();
  });
}

/**
 * Spawn the Python backend and wait for the login URL.
 *
 * The backend emits structured JSONL events to stdout (via --format jsonl)
 * and human-readable log messages to stderr. We parse stdout for the
 * login_url event and log everything to the log file.
 *
 * In dev mode, uses `uv run --package minds` from the monorepo root so
 * the workspace venv (with all plugins) is used directly.
 *
 * Returns a promise that resolves with { loginUrl, port } when the backend
 * is ready, or rejects if the process exits before emitting the URL.
 */
function startBackend(onProgress, onNotification, onAuthEvent, onMngrForwardStarted) {
  return new Promise((resolve, reject) => {
    let isResolved = false;

    findAvailablePort().then(async (port) => {
      // A stale backend from a previous app instance may still be shutting
      // down on this port. Wait up to 6 seconds for it to release the port.
      const isFree = await waitForPortFree(port);
      if (!isFree) {
        // Port is still occupied -- pick a different one.
        port = await findAvailablePort();
      }
      const logDir = paths.getLogDir();

      // Ensure log directory exists
      fs.mkdirSync(logDir, { recursive: true });

      const logFile = path.join(logDir, 'minds.log');
      // Rotate + gzip minds.log like the other logs (it was previously an
      // unbounded append stream that grew to hundreds of MB). Same write/end
      // surface as the old fs.createWriteStream, so the callers below are
      // unchanged.
      const logStream = createRotatingLogStream({ filePath: logFile });

      onProgress('Starting Minds...');

      let uvBin, args, cwd, env;

      const mindsRootName = paths.getMindsRootName();
      const mngrHostDir = paths.getMngrHostDir();
      const mngrPrefix = paths.getMngrPrefix();
      // Forwarded to the Python backend so Sentry tags reports with the desktop
      // app version (release) and the git SHA the build was cut from.
      const { releaseId, gitSha } = getBuildMetadata();
      // When build.js embedded a client.toml + root_name pair (production
      // / staging / beta packaged builds), pass --config-file explicitly
      // so the backend doesn't have to fall back to MINDS_CLIENT_CONFIG_PATH.
      // Dev-mode builds (no bundle) inherit MINDS_CLIENT_CONFIG_PATH from
      // the user's activated shell instead; the backend refuses to start
      // if neither path is set.
      const bundledClientConfig = paths.getBundledClientConfigPath();
      const configFileArgs = bundledClientConfig ? ['--config-file', bundledClientConfig] : [];

      // desync is not staged on win32, and naming a missing file here would make the
      // backend exec that instead of falling back to a PATH lookup.
      const desyncPath = paths.getDesyncPath();
      const hasBundledDesync = fs.existsSync(desyncPath);
      const limaImageToolEnv = {
        ...(hasBundledDesync ? { MINDS_DESYNC_BINARY: desyncPath } : {}),
      };

      if (paths.isDev()) {
        // Dev mode: use system uv with the monorepo workspace venv
        uvBin = 'uv';
        args = [
          'run', '--package', 'minds',
          'minds', '-vv', '--format', 'jsonl',
          '--log-file', path.join(logDir, 'minds-events.jsonl'),
          'run',
          '--host', '127.0.0.1',
          '--port', String(port),
          '--no-browser',
          ...configFileArgs,
        ];
        cwd = paths.getMonorepoRoot();
        env = {
          ...process.env,
          MINDS_ELECTRON: '1',
          MINDS_ROOT_NAME: mindsRootName,
          MNGR_HOST_DIR: mngrHostDir,
          MNGR_PREFIX: mngrPrefix,
          MINDS_LATCHKEY_BINARY: paths.getLatchkeyPath(),
          MINDS_LATCHKEY_DIRECTORY: paths.getLatchkeyDirectory(),
          // The prestart hook (ensure-binaries.js) stages resources/desync/ before the
          // dev app launches. Dev mode inherits the developer's PATH untouched, so the
          // bundled binary is only reachable by absolute path -- without this the
          // fast-create path would need a system-wide desync.
          ...limaImageToolEnv,
          // The prestart hook (ensure-binaries.js) downloads the pinned
          // restic into resources/restic/; without this the backend falls
          // back to `restic` on PATH, which only works on machines that
          // happen to have a system restic (packaged mode always sets it).
          MINDS_RESTIC_BINARY: paths.getResticPath(),
          MINDS_RELEASE_ID: releaseId,
          MINDS_GIT_SHA: gitSha,
        };
      } else {
        // Packaged mode: use bundled uv with standalone pyproject
        const uvPath = paths.getUvPath();
        const uvBinDir = paths.getUvBinDir();
        const gitBinDir = paths.getGitBinDir();
        const limaBinDir = paths.getLimaBinDir();
        const desyncBinDir = paths.getDesyncBinDir();
        const bundledBinDirs = [uvBinDir, gitBinDir, limaBinDir, desyncBinDir];
        const uvCacheDir = paths.getUvCacheDir();
        const uvPythonDir = paths.getUvPythonDir();
        const pyprojectDir = paths.getPyprojectDir();

        uvBin = uvPath;
        args = [
          'run', '--project', pyprojectDir,
          // --active makes uv use VIRTUAL_ENV (~/.minds/.venv) instead of
          // <project>/.venv, which is inside the signed .app bundle and
          // read-only on macOS. Without this, `uv run` tries to create
          // .venv inside the bundle and fails with "Operation not permitted".
          '--active',
          // -v sets the stderr threshold to DEBUG so
          // the subprocess's mngr forward / agent-creation / restic
          // lifecycle traces land in minds.log for bug reports.
          'minds', '-v', '--format', 'jsonl',
          // The --log-file JSONL sink is already DEBUG regardless of -v.
          '--log-file', path.join(logDir, 'minds-events.jsonl'),
          'run',
          '--host', '127.0.0.1',
          '--port', String(port),
          '--no-browser',
          ...configFileArgs,
        ];
        cwd = pyprojectDir;
        // LaunchServices-started apps on macOS inherit a minimal PATH
        // without /opt/homebrew/bin or /usr/local/bin. Append both so
        // user-installed tools (`docker` from Docker Desktop, etc.) are
        // reachable. Bundled uv/git/lima are prepended via their own
        // absolute paths above. On Linux these dirs are also conventional
        // (`/usr/local/bin` is std) and the append is harmless either way.
        const systemPath = process.env.PATH || '';
        const homebrewPaths = ['/opt/homebrew/bin', '/usr/local/bin'].filter(
          (p) => !systemPath.split(':').includes(p)
        ).join(':');
        const augmentedSystemPath = homebrewPaths
          ? `${systemPath}:${homebrewPaths}`
          : systemPath;
        env = {
          ...process.env,
          PATH: `${bundledBinDirs.join(':')}:${augmentedSystemPath}`,
          UV_CACHE_DIR: uvCacheDir,
          UV_PYTHON_INSTALL_DIR: uvPythonDir,
          MINDS_ELECTRON: '1',
          MINDS_ROOT_NAME: mindsRootName,
          MNGR_HOST_DIR: mngrHostDir,
          MNGR_PREFIX: mngrPrefix,
          MINDS_LATCHKEY_BINARY: paths.getLatchkeyPath(),
          MINDS_LATCHKEY_DIRECTORY: paths.getLatchkeyDirectory(),
          MINDS_RESTIC_BINARY: paths.getResticPath(),
          ...limaImageToolEnv,
          // Tell the packaged latchkey shim which Electron binary to use as Node.
          MINDS_ELECTRON_EXEC_PATH: process.execPath,
          // Set VIRTUAL_ENV to the per-user venv so `uv run --active` uses
          // it. Without this, uv falls back to <project>/.venv which is
          // inside the signed .app bundle (read-only on macOS).
          VIRTUAL_ENV: paths.getVenvDir(),
          MINDS_RELEASE_ID: releaseId,
          MINDS_GIT_SHA: gitSha,
        };
      }

      const child = spawn(uvBin, args, {
        env,
        cwd,
        stdio: ['ignore', 'pipe', 'pipe'],
      });

      backendProcess = child;

      // Parse JSONL events from stdout for the login URL
      let stdoutBuffer = '';

      // Flush any buffered (newline-less) trailing stdout to the log so a final
      // fragment emitted right before exit is never dropped. Redacted like every
      // other logged line.
      const flushStdoutBufferToLog = () => {
        if (stdoutBuffer) {
          logStream.write(redactStdoutLineForLog(stdoutBuffer) + '\n');
          stdoutBuffer = '';
        }
      };

      child.stdout.on('data', (data) => {
        const text = data.toString();
        stdoutBuffer += text;

        const lines = stdoutBuffer.split('\n');
        // Keep the last incomplete line in the buffer
        stdoutBuffer = lines.pop() || '';

        for (const line of lines) {
          // Log every complete line, but mask secret-bearing event fields
          // (the mngr-forward preauth cookie) first: minds.log is uploaded with
          // bug reports, so that reusable session token must never land in it.
          // The in-process handlers below still parse the original line, so they
          // receive the real values.
          logStream.write(redactStdoutLineForLog(line) + '\n');
          if (!line.trim()) continue;
          try {
            const event = JSON.parse(line);
            if (event.event === 'login_url' && event.login_url) {
              if (!isResolved) {
                isResolved = true;
                // Wait for the server to actually start listening before resolving
                waitForPort('127.0.0.1', port).then(() => {
                  resolve({ loginUrl: event.login_url, port });
                }).catch((err) => {
                  reject(new Error(`Backend emitted login URL but server never became ready: ${err.message}`));
                });
              }
            } else if (event.event === 'notification' && event.message && onNotification) {
              onNotification(event);
            } else if ((event.event === 'auth_success' || event.event === 'auth_required') && onAuthEvent) {
              onAuthEvent(event);
            } else if (event.event === 'mngr_forward_started' && onMngrForwardStarted) {
              onMngrForwardStarted(event);
            }
          } catch {
            // Not valid JSON -- just log it
          }
        }
      });

      // Stderr is human-readable logging -- capture to log file and console.
      // The dev-mode forward to process.stderr can throw EPIPE if the parent
      // (e.g. a `just` recipe whose stdout was piped through `head`) goes
      // away while the backend is still emitting log lines. We swallow the
      // error: the log file is the durable record, and a broken parent pipe
      // should never bring down the Electron main process.
      child.stderr.on('data', (data) => {
        const text = data.toString();
        logStream.write(text);
        if (!isResolved) {
          // Surface uv's freshest progress line on the splash so cold launch doesn't look frozen.
          const latest = text.split('\n').map(l => l.trim()).filter(Boolean).pop();
          if (latest) onProgress(latest);
        }
        if (paths.isDev()) {
          try {
            process.stderr.write(text);
          } catch (err) {
            if (err && err.code !== 'EPIPE') {
              throw err;
            }
          }
        }
      });

      child.on('error', (err) => {
        flushStdoutBufferToLog();
        logStream.end();
        if (!isResolved) {
          isResolved = true;
          reject(new Error(`Failed to start backend: ${err.message}`));
        }
      });

      child.on('exit', (code) => {
        backendProcess = null;
        flushStdoutBufferToLog();
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
      console.log('[shutdown] No backend process to shut down');
      resolve();
      return;
    }

    const child = backendProcess;
    let isExited = false;
    const startTime = Date.now();

    child.on('exit', (code, signal) => {
      const elapsed = Date.now() - startTime;
      console.log(`[shutdown] Backend exited after ${elapsed}ms (code=${code}, signal=${signal})`);
      isExited = true;
      backendProcess = null;
      resolve();
    });

    console.log(`[shutdown] Sending SIGTERM to backend (PID ${child.pid})`);
    child.kill('SIGTERM');

    setTimeout(() => {
      if (!isExited) {
        const elapsed = Date.now() - startTime;
        console.log(`[shutdown] Backend still alive after ${elapsed}ms, sending SIGKILL`);
        try {
          child.kill('SIGKILL');
        } catch {
          // Process may have already exited
        }
      }
      // Resolve after SIGKILL attempt regardless
      setTimeout(() => {
        if (!isExited) {
          const elapsed = Date.now() - startTime;
          console.log(`[shutdown] Backend did not exit after SIGKILL (${elapsed}ms), giving up`);
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
