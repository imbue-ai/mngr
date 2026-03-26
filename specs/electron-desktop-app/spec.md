# Minds Desktop App -- Detailed Spec

## Motivation

Minds is currently distributed as a Python CLI tool installed via a shell script. Users must have Python, git, and uv available on their system, then run `mind forward` to start a local web server, which they access via a browser tab. This works, but introduces friction:

- **Installation prerequisites**: Users need Python >= 3.11, git, and uv installed and working. This is a nontrivial ask for non-developers.
- **No native app experience**: The app lives in a browser tab. There is no dock icon, no window management, no native notifications.
- **No auto-updates**: Users must manually update via the install script.
- **No code signing**: On macOS, unsigned CLI tools trigger Gatekeeper warnings. The install script must instruct users to bypass security dialogs.

The goal of this project is to package Minds as a standalone desktop app that a user can download, install, and run with zero prerequisites. The existing Python backend is preserved as-is -- this is packaging, not a rewrite.

## Architecture

```
+--------------------------------------------------+
|  Electron Shell (main.js)                        |
|                                                  |
|  1. app.requestSingleInstanceLock()              |
|  2. env-setup.js: run bundled uv sync            |
|  3. backend.js: spawn uv run mind forward        |
|  4. Parse login URL from stderr (JSONL)          |
|  5. BrowserWindow -> http://127.0.0.1:<port>/... |
|  6. On close: SIGTERM child, wait, SIGKILL       |
+--------------------------------------------------+
        |                       ^
        | spawn child process   | stderr (JSONL events)
        v                       |
+--------------------------------------------------+
|  Python Backend (unchanged)                      |
|                                                  |
|  mind forward --host 127.0.0.1                   |
|               --port <random>                    |
|               --log-format jsonl                 |
|                                                  |
|  FastAPI forwarding server                       |
|  Auth (one-time code, signed cookies)            |
|  Agent discovery (mng list, mng events)          |
|  HTTP/WebSocket proxying to agents               |
+--------------------------------------------------+
        |
        | subprocess: mng list, mng events, mng create, git clone, ...
        v
+--------------------------------------------------+
|  Bundled Binaries                                |
|  - uv (Python env management)                   |
|  - git (repo operations)                         |
|  (both platform-specific, in app resources dir)  |
+--------------------------------------------------+
```

The Electron shell is deliberately thin. It does four things:

1. **Environment setup**: Runs `uv sync` on launch to ensure the Python environment is current.
2. **Backend lifecycle**: Spawns and monitors the `mind forward` process.
3. **Auth handshake**: Parses the login URL from the backend's structured stderr output and navigates to it.
4. **Window management**: Displays the backend's web UI in a BrowserWindow and handles close/crash.

Everything else -- agent creation, discovery, proxying, authentication, the web UI itself -- remains in the Python backend, unchanged.

## Packaging and Distribution

**ToDesktop for Electron** handles the build pipeline:

- Native installers: .dmg for macOS, .AppImage for Linux (Windows later)
- Code signing and macOS notarization (ToDesktop provides certificates)
- Auto-update infrastructure (background download, apply on next launch)
- Hosted download page with OS-detecting links

**pnpm** is used for all JS package management.

**Build flow:**

1. `pnpm build` assembles the Electron app:
   - Copies `electron/pyproject.toml` and its lockfile into the resources directory
   - Downloads platform-specific `uv` and `git` binaries into the resources directory
   - Packages the Electron code (main.js, preload.js, HTML pages, assets)
2. `pnpm exec todesktop build` uploads the assembled app to ToDesktop, which:
   - Builds native installers for each target platform
   - Signs and notarizes the app
   - Publishes to the update server

**Python packages are not bundled.** The app ships a `pyproject.toml` + lockfile that declares `imbue-minds` as a dependency. On first launch, `uv sync` installs Python and all packages from PyPI. This means:

- `imbue-minds` must be published to PyPI (it transitively depends on `mng`, which is already published)
- The lockfile pins exact versions, so every user gets the same environment
- Updating the Electron app (via ToDesktop) delivers a new lockfile, and the next `uv sync` picks up the new versions

## Bundled Binaries

### uv

- Platform-specific binary (~30MB)
- Downloaded during `pnpm build` for the target platform
- Placed in the app's resources directory (outside asar archive, since it must be executable)
- Handles: Python installation, venv creation, dependency resolution and installation
- The build script should download from `https://github.com/astral-sh/uv/releases`

### git

- Platform-specific binary/distribution
- Downloaded during `pnpm build` for the target platform
- Placed in the app's resources directory
- Each platform build (macOS arm64, macOS x64, Linux x64) bundles the appropriate git
- Source: platform package managers or static builds (e.g., `git-for-distribution` project, or Homebrew bottles for macOS)

Both binaries are made available to the Python backend by prepending their directories to `PATH` in the child process environment.

## Electron App Structure

All Electron code lives in `apps/minds/` alongside the existing Python code:

```
apps/minds/
  package.json              # pnpm, Electron, ToDesktop config
  pnpm-lock.yaml
  todesktop.json            # ToDesktop for Electron config
  electron/
    main.js                 # Electron main process entry point
    preload.js              # Preload script (minimal for now)
    paths.js                # Platform-aware path resolution for bundled binaries
    env-setup.js            # uv sync runner with progress reporting
    backend.js              # Python backend process manager
    loading.html            # Loading/setup screen
    error.html              # Error screen with details toggle
    assets/
      icon.svg              # Placeholder brain icon
      icon.png              # Generated from SVG for Electron (multiple sizes)
    pyproject.toml          # Standalone: declares imbue-minds dependency
    uv.lock                 # Pinned lockfile for reproducible installs
  # ... existing Python code unchanged ...
  imbue/minds/
  pyproject.toml            # Existing monorepo pyproject.toml (unchanged)
  ...
```

### main.js -- Electron Main Process

Responsibilities:

- **Single instance lock**: Call `app.requestSingleInstanceLock()`. If lock is not acquired, focus the existing window and `app.quit()`.
- **Startup sequence**:
  1. Create a BrowserWindow and load `loading.html`
  2. Run env-setup (uv sync) -- update loading screen with progress
  3. Find an available port
  4. Spawn the backend process
  5. Parse stderr for the login URL JSONL event
  6. Navigate the BrowserWindow to the login URL
- **Window close**: Send SIGTERM to the backend child process. Wait up to 5 seconds. If still alive, send SIGKILL.
- **Backend crash**: Detect child process exit. Load `error.html` with the last N lines from the log file. Provide a "Restart" button that re-runs the startup sequence from step 3.

### paths.js -- Path Resolution

Resolves paths to bundled resources accounting for:

- **asar packaging**: Electron packs app code into an asar archive, but binary executables must be outside it. Use `process.resourcesPath` to locate the unpacked resources directory.
- **Platform differences**: macOS places resources in `Contents/Resources/`, Linux in `resources/`.
- **Development mode**: When running via `electron .` during development, paths resolve relative to the project directory.

Exports:

- `getUvPath()` -- path to the bundled `uv` binary
- `getGitPath()` -- path to the bundled `git` binary
- `getDataDir()` -- `~/.minds/` (same as the CLI default)
- `getUvCacheDir()` -- `~/.minds/.uv-cache/`
- `getUvPythonDir()` -- `~/.minds/.uv-python/`
- `getLogDir()` -- `~/.minds/logs/`
- `getPyprojectDir()` -- directory containing the bundled pyproject.toml + lockfile
- `getVenvDir()` -- `~/.minds/.venv/`

### env-setup.js -- Environment Setup

Runs `uv sync` using the bundled `uv` binary and the bundled `pyproject.toml` + lockfile.

```
uv sync --project <pyproject-dir> --python-preference only-managed
```

- `--project` points to the bundled pyproject.toml directory
- `--python-preference only-managed` tells uv to install Python itself rather than looking for a system Python
- The venv is created at `~/.minds/.venv/`
- Environment variables `UV_PYTHON_INSTALL_DIR` and `UV_CACHE_DIR` are set to locations within `~/.minds/` so all uv state is self-contained (no shared system cache)

**Progress reporting**: Parses uv's stderr output for progress indicators. Sends IPC messages to the renderer process (loading.html) to update the progress display. On failure, captures the full stderr output for the error screen.

**Idempotency**: `uv sync` is inherently idempotent. If the venv already matches the lockfile, it completes in <1 second. If dependencies changed (new Electron release with updated lockfile), it installs the diff.

### backend.js -- Backend Process Manager

Spawns and manages the Python backend as a child process.

**Port selection**: Uses Node's `net.createServer()` trick:
```js
const server = net.createServer();
server.listen(0);
const port = server.address().port;
server.close();
```

**Process spawn**:
```js
const child = spawn(uvPath, ['run', 'mind', 'forward',
  '--host', '127.0.0.1',
  '--port', String(port),
  '--log-format', 'jsonl'
], {
  env: {
    ...process.env,
    PATH: `${uvBinDir}:${gitBinDir}:${process.env.PATH}`,
    MINDS_ELECTRON: '1',
    VIRTUAL_ENV: venvDir,
    UV_CACHE_DIR: uvCacheDir,
    UV_PYTHON_INSTALL_DIR: uvPythonDir,
  },
  cwd: pyprojectDir,
  stdio: ['ignore', 'pipe', 'pipe'],
});
```

**Stderr parsing**: Reads stderr line-by-line. Each line is attempted as JSON parse. Watches for a JSONL event with a `login_url` field. All stderr output is also appended to the log file at `~/.minds/logs/minds.log`.

**Health monitoring**: After receiving the login URL, the backend is considered "starting". The BrowserWindow navigates to the login URL. If the child process exits unexpectedly, the error screen is shown.

**Shutdown**: Exported `shutdown()` function sends SIGTERM to the child process, sets a 5-second timeout, then sends SIGKILL if the process hasn't exited.

### loading.html -- Loading Screen

A simple HTML page displayed during startup. Shows:

- App name/logo (placeholder brain icon)
- Status text that updates via IPC:
  - "Setting up environment..." (during `uv sync` on first launch)
  - "Starting Minds..." (during backend startup)
- A progress indicator (spinner or progress bar)

Styled to be visually clean and minimal. No external dependencies -- all CSS is inline.

### error.html -- Error Screen

Displayed when env-setup or backend startup fails. Shows:

- A brief message: "Something went wrong" or "Setup failed -- you may not be connected to the internet" (for `uv sync` failures)
- A "Retry" button that sends an IPC message to main.js to restart the startup sequence
- A "Show details" toggle that reveals the raw error output / last N log lines
- A "View full log" link that opens the log file in the system's default text editor

## Changes to Existing Python Code

### cli/forward.py -- Add `--log-format` Option

Add a `--log-format` option to the `forward` command:

```python
@click.option(
    "--log-format",
    type=click.Choice(["text", "jsonl"]),
    default="text",
    help="Log output format (jsonl for machine-parseable structured events)",
)
```

When `jsonl` is selected, `setup_logging()` is called with a format parameter that switches the stderr sink to emit structured JSONL lines instead of human-readable text. This follows the same pattern as mng's file-based JSONL logging (via `make_jsonl_file_sink` from `imbue_common`), but directed to stderr instead of a file.

The JSONL events use the same envelope structure as mng's file logs: `timestamp`, `type`, `level`, `message`, plus any `extra` context. This allows Electron to parse specific event types from the stream.

### forwarding_server/runner.py -- Electron Mode

When the environment variable `MINDS_ELECTRON=1` is set:

- **Skip `webbrowser.open()`**: Do not spawn the thread that opens the browser. The Electron BrowserWindow handles navigation.
- **Auth remains fully intact**: The one-time code is still generated and emitted to stderr. Auth middleware still protects all routes. The Electron app authenticates by navigating to the login URL, just as a browser would.

The login URL emission should use a structured log event with identifiable fields so `backend.js` can parse it:

```python
logger.info(
    "Login URL ready",
    login_url=login_url,
)
```

In JSONL format, this produces a line like:
```json
{"timestamp": "...", "type": "minds", "level": "INFO", "message": "Login URL ready", "extra": {"login_url": "http://127.0.0.1:54321/login?one_time_code=abc..."}}
```

Electron's `backend.js` parses each JSONL line and checks for the `login_url` key in `extra`.

### utils/logging.py -- JSONL Stderr Sink

Add a `jsonl` format mode to `setup_logging()`:

- When format is `text` (default): current behavior, human-readable colored output to stderr
- When format is `jsonl`: use a JSONL sink on stderr, reusing `make_jsonl_file_sink()`'s serialization logic but writing to stderr instead of a file

This is a small addition -- the JSONL serialization logic already exists in `imbue_common.logging`. The new code just needs to apply it to a stderr sink rather than a file sink.

## Data Directory Layout

```
~/.minds/
  .venv/                    # uv-managed Python virtual environment
  .uv-cache/                # uv package cache (self-contained)
  .uv-python/               # uv-managed Python installations
  logs/
    minds.log               # Backend log file (rotated)
  auth/
    signing_key             # Cookie signing key (existing)
    one_time_codes.json     # One-time codes (existing)
  <agent-id>/               # Per-agent directories (existing)
    ...
```

The venv at `~/.minds/.venv/` is managed entirely by `uv sync`. It should not be manually modified. If corrupted, deleting it and relaunching the app will recreate it.

Log rotation should follow the same conventions as mng's file logging. The log file is the primary debugging tool when the app misbehaves -- it is accessible via the app menu ("View Logs") and shown in the error screen.

## Port Selection

The app uses a random available port instead of the hardcoded 8420 default. This avoids conflicts with:

- Another instance of `mind forward` running in a terminal
- Other services that happen to use port 8420

The port is ephemeral and changes each launch. This is acceptable because:

- Only the Electron BrowserWindow accesses it (no bookmarks, no external links)
- The Electron shell knows the port and passes it to the BrowserWindow
- Auth still protects the server from other local processes

## Single Instance Enforcement

Electron's `app.requestSingleInstanceLock()` is used to prevent multiple instances:

```js
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
  return;
}

app.on('second-instance', () => {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  }
});
```

This uses an OS-level lock (file lock on Linux, named mutex on macOS). No custom lock file management is needed.

## Process Lifecycle

### Startup

1. Electron `app.whenReady()` fires
2. Acquire single-instance lock (or quit)
3. Create BrowserWindow, load `loading.html`
4. Run `env-setup.js`:
   - Spawn `uv sync --project <dir> --python-preference only-managed`
   - Parse stderr for progress, forward to loading screen via IPC
   - On failure: load `error.html` with error details, stop
5. Run `backend.js`:
   - Find available port
   - Spawn `uv run mind forward --host 127.0.0.1 --port <port> --log-format jsonl`
   - Set `PATH` to include bundled uv and git directories
   - Set `MINDS_ELECTRON=1`
   - Set `VIRTUAL_ENV` to `~/.minds/.venv/`
   - Working directory: the bundled pyproject.toml directory
6. Parse stderr for JSONL login URL event
7. Navigate BrowserWindow to login URL
8. Auth completes (one-time code consumed, session cookie set), server redirects to landing page
9. User sees the Minds web UI

### Shutdown

1. User closes the window (or quits the app)
2. `window-all-closed` event fires
3. Send SIGTERM to the backend child process
4. Start a 5-second timer
5. If child exits within 5 seconds: clean exit
6. If timer expires: send SIGKILL
7. `app.quit()`

### Crash Recovery

1. Backend child process exits unexpectedly (non-zero exit code)
2. Electron detects via `child.on('exit', ...)`
3. Load `error.html` with:
   - Message: "Minds stopped unexpectedly"
   - Last 50 lines from the log file
   - "Restart" button: re-runs startup from step 5 (backend spawn)
   - "Show details" toggle for full error output

## Updates

The update flow is:

1. Developer bumps the `imbue-minds` version in `electron/pyproject.toml` and regenerates the lockfile
2. Developer commits and runs `pnpm exec todesktop build`
3. ToDesktop builds new installers, signs them, publishes to update server
4. Running Minds apps check for updates in the background (ToDesktop handles this)
5. On next launch, the new Electron app is loaded
6. The new app's `uv sync` sees the updated lockfile and installs the new `imbue-minds` version
7. The user sees a slightly longer loading screen while deps update, then the app is current

This coupling means Python and Electron updates are atomic from the user's perspective. There is no version skew between the Electron shell and the Python backend.

## Standalone pyproject.toml

The file at `electron/pyproject.toml` is separate from the monorepo's `apps/minds/pyproject.toml`. It exists solely to tell `uv sync` what to install inside the Electron app:

```toml
[project]
name = "minds-desktop"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "imbue-minds>=0.1.0",
]
```

This is intentionally minimal. `imbue-minds` transitively pulls in `mng`, `mng-claude-mind`, and all other Python dependencies. The lockfile (`uv.lock`) pins everything.

When cutting a new release, the developer updates the version pin (e.g., `imbue-minds>=0.2.0`) and regenerates the lockfile with `uv lock`. This lockfile is committed and shipped in the Electron app bundle.

## App Identity

- **Name**: "Minds"
- **Icon**: Placeholder brain SVG (to be replaced with a real icon later)
- **Bundle ID** (macOS): `com.imbue.minds` (or similar -- needed for code signing)
- **Linux desktop entry**: `minds.desktop` (generated by ToDesktop or the build script)

## Platforms

- **macOS**: Universal binary (arm64 + x64 in one .app) -- necessary since users may not know their architecture
- **Linux**: x64 (AppImage format)
- **Windows**: Deferred to a later release
