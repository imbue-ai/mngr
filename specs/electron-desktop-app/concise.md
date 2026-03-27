# Minds Desktop App

Package apps/minds as a standalone, installable Electron desktop app.

* Bundle the existing Python backend as-is inside the Electron app -- no backend rewrite
* Use ToDesktop for Electron for packaging, code signing, installers, and auto-updates (macOS + Linux, Windows later)
* Bundle `uv` binary + pyproject.toml + lockfile; on first launch, `uv sync` installs Python + all deps from PyPI (including `mngr` and `imbue-minds`)
* Bundle a platform-appropriate git binary so there are no external prerequisites -- the app is fully self-contained
* Publish `imbue-minds` (the minds app) to PyPI so it can be installed via `uv` just like `mngr` -- no source bundling needed
* Use pnpm for all JS package management
* Accept first-launch delay for `uv sync`; show a progress indicator
* Python backend updates are coupled to Electron updates via ToDesktop -- the bundle contains pinned hashes, `uv sync` enforces them
* Electron layer starts thin (BrowserWindow pointing at localhost) but will add native features over time (system tray, notifications, menu bar, shortcuts)
* Silent startup: Electron launches Python backend in background, shows loading screen, displays UI once server is ready
* Closing the window stops the forwarding server; agents continue running independently
* Electron code lives alongside Python code in `apps/minds/`

## Overview

* Minds is currently a Python CLI app (`mind forward`) that launches a FastAPI forwarding server on localhost:8420, accessed via browser. This spec wraps it in Electron to make it installable as a native desktop app with zero prerequisites.
* The Electron app is a thin shell: it manages the lifecycle of the Python backend (start on launch, stop on close) and displays the existing web UI in a BrowserWindow. No backend code is rewritten.
* The bundled `uv` binary handles Python environment setup. A platform-appropriate `git` binary is included. All Python packages (including `mngr` and `imbue-minds`) are installed from PyPI via `uv sync` -- no source code is bundled, only pyproject.toml + lockfile.
* ToDesktop for Electron handles the painful parts: native installers (.dmg, .AppImage), code signing, notarization, and auto-updates. This avoids weeks of build infrastructure work.
* The app uses a random available port (not hardcoded 8420) to avoid conflicts with other local services or a separately-running `mind forward`.
* First launch (and every launch) runs `uv sync` to set up the Python environment (~10-30s with progress indicator the first time). Subsequent launches also run uv sync, thus picking up any updates to dependencies that come with new Electron releases.
* Single-instance enforcement: uses Electron's `app.requestSingleInstanceLock()` -- launching a second instance focuses the existing window and exits the new process.

## Expected Behavior

**Installation:**
* User downloads a .dmg (macOS) or .AppImage (Linux) from a ToDesktop-hosted download page
* macOS: drag to Applications, launch from dock. Linux: make executable, run directly or integrate with desktop environment
* No other software needs to be installed -- no Python, no git, no uv, no mngr

**First launch:**
* App opens a window showing a loading/setup screen: "Setting up Minds..." with a progress indicator
* Behind the scenes, the bundled `uv` binary runs `uv sync` to install Python and all dependencies from PyPI into a venv inside the app's data directory
* Once the environment is ready, the app starts the forwarding server on a random available port
* The backend emits the one-time auth login URL as a structured JSONL event on stderr; Electron parses it and automatically navigates the BrowserWindow to that URL, completing auth transparently
* The loading screen transitions to the existing Minds web UI (landing page)

**Subsequent launches:**
* App opens a window showing a brief loading screen: "Starting Minds..."
* Call bundled `uv sync` to check that the environment is current (fast no-op if nothing changed), then starts the forwarding server
* Loading screen transitions to the web UI within 1-2 seconds

**Normal usage:**
* The app behaves identically to the current browser-based experience: create agents, chat, manage lifecycle
* All proxying, WebSocket support, service workers, etc. work exactly as they do today

**Closing the app:**
* Closing the window sends SIGTERM to the forwarding server process
* The forwarding server shuts down (stops stream manager, cleans up SSH tunnels)
* Agents themselves continue running (they are separate processes managed by mngr)
* Reopening the app reconnects to existing running agents

**Updates:**
* ToDesktop pushes updates in the background
* On next launch, the new version is applied automatically
* Since the bundle includes updated source + lockfile, `uv sync` picks up any dependency changes
* Updates are seamless -- the user just sees the loading screen slightly longer if deps changed

**Errors:**
* If `uv sync` fails (e.g., network issues on first launch), the loading screen shows a message like "Setup failed -- you may not be connected to the internet" with a "Retry" button and a toggleable "Show details" section that displays the actual error output
* If the forwarding server crashes, the app shows an error screen with the last few log lines and a "Restart" button
* All backend logs are written to a log file in the app's data directory, accessible via a "View Logs" menu item (use similar logging conventions to how mngr already writes logs)
* A collapsible log panel is accessible from the app menu for debugging

**System tray (future):**
* Not in initial release, but the architecture supports adding: minimize to tray, tray icon showing agent status, reopen from tray

## Changes

**New files in `apps/minds/`:**

* `package.json` -- Electron + ToDesktop dependencies and build configuration (managed with pnpm)
* `electron/main.js` -- Electron main process: window management, backend lifecycle, port selection
* `electron/preload.js` -- Preload script for secure context bridge (if needed for native features later)
* `electron/loading.html` -- Loading/setup screen shown during backend startup
* `electron/error.html` -- Error screen shown when backend fails to start
* `electron/backend.js` -- Module that manages the Python backend subprocess: spawns `uv run mind forward --port <port>`, monitors health, handles shutdown
* `electron/env-setup.js` -- Module that runs `uv sync` on first launch (or when deps change), reports progress
* `electron/paths.js` -- Resolves paths to bundled binaries (uv, git) and data directories, accounting for platform differences and asar packaging
* `todesktop.json` -- ToDesktop for Electron configuration
* `electron/assets/icon.svg` -- Placeholder app icon (brain SVG), used for dock/taskbar/window icon
* `electron/pyproject.toml` -- Standalone pyproject.toml that declares `imbue-minds` (and transitively `mngr`) as a dependency, with a lockfile pinning exact versions. This is what `uv sync` operates on inside the app -- it is separate from the monorepo's pyproject.toml.

**Changes to existing Python code:**

* `forwarding_server/runner.py` -- Skip the automatic `webbrowser.open()` call when launched in "electron mode" (detected via environment variable like `MINDS_ELECTRON=1`). Auth itself remains fully intact -- the one-time code is still generated and emitted as a structured JSONL event on stderr so Electron can capture and use it.
* `cli/forward.py` -- Add a `--log-format` option (default: `text`, also supports `jsonl`) following the same pattern as `mngr`'s logging options. When `jsonl`, structured events (including the login URL) are emitted to stderr as parseable JSONL lines. Electron launches with `--log-format jsonl` to reliably parse events.
* `cli/forward.py` -- No other changes needed; the existing `--host` and `--port` flags are sufficient for Electron to control binding.

**Bundled binaries (vendored or downloaded at build time):**

* `uv` -- Platform-specific binary, ~30MB. Downloaded for the target platform during the build step. Placed in the app's resources directory.
* `git` -- Platform-specific binary/distribution. Downloaded for the target platform during the build step. Each platform build (macOS, Linux) bundles the appropriate git for that OS.

**Build and distribution:**

* ToDesktop CLI (`@todesktop/cli`) integrated into the build pipeline
* `pnpm build` produces the Electron app with bundled uv, git, and a pyproject.toml + lockfile (dependencies are installed from PyPI at runtime via `uv sync`)
* `pnpm exec todesktop build` uploads to ToDesktop for signing, notarization, and distribution
* CI/CD publishes new versions by running the ToDesktop build

**Data directory layout:**

* `~/.minds/` remains the data directory for agent state (unchanged)
* `~/.minds/.venv/` -- uv-managed Python virtual environment
* `~/.minds/logs/` -- Backend log files (rotated)
* The bundled (js) source code lives inside the app bundle (e.g., `Contents/Resources/` on macOS), not in `~/.minds/` (though obviously a bunch of python code will end up there)

**Port selection:**

* Electron's `backend.js` finds a random available port using Node's `net.createServer().listen(0)` trick
* Passes the port to the backend via `mind forward --port <port>`
* Points the BrowserWindow at `http://127.0.0.1:<port>`
* The port is ephemeral and changes each launch -- this is fine since only the Electron window accesses it

**Process lifecycle:**

* Electron main process spawns `uv run mind forward --host 127.0.0.1 --port <port>` as a child process
* `PATH` is modified to include the directories containing the bundled `uv` and `git` binaries
* Backend is launched with `--log-format jsonl`; Electron parses stderr for structured JSONL events (e.g., the login URL). All output is also written to a log file.
* Electron watches stderr for the JSONL login URL event. Once received, it navigates the BrowserWindow to that URL (which authenticates and redirects to the main UI).
* On window close: sends SIGTERM to the child process, waits up to 5s, then SIGKILL if still alive
* On crash: detects child process exit, shows error screen with restart option
