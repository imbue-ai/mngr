# Desktop App

Minds ships as a standalone desktop application built with Electron and distributed via [ToDesktop](https://www.todesktop.com/). The desktop app wraps the existing Python backend -- no code changes are needed to the web UI or agent system.

## How it works

The Electron shell is deliberately thin. It handles four things:

1. **Environment setup**: Runs `uv sync` on launch to install/update the Python environment
2. **Backend lifecycle**: Spawns and monitors the `minds forward` process
3. **Auth handshake**: Parses the login URL from stdout and navigates to it
4. **Window management**: Displays the backend's web UI in a native window

Everything else -- agent creation, discovery, proxying, authentication, the web UI -- remains in the Python backend, unchanged. See [overview.md](./overview.md) for details on the desktop client architecture.

### App shell

The Electron window uses a frameless window (`frame: false` on Linux/Windows, `titleBarStyle: 'hiddenInset'` with `trafficLightPosition` on macOS). A custom title bar is injected into every backend page via `webContents.insertCSS()` and `webContents.executeJavaScript()` on the `dom-ready` event. The title bar uses `-webkit-app-region: drag` so the entire bar acts as a window drag handle, with buttons opted out via `no-drag`. The title bar provides:

- **Navigation**: Back/forward buttons using `history.back()`/`history.forward()`
- **Page title**: Tracks `document.title` via MutationObserver
- **Open in browser**: Opens the current URL in the system browser
- **Window controls**: Minimize/maximize/close buttons (on Linux/Windows; macOS uses native traffic lights)

A separate `shell.html` page handles the loading spinner and error screen during startup.

When accessing an agent URL in a regular browser (not the Electron app), the Python backend wraps the content in a lightweight info bar showing the agent name, host, and application name.

### Startup sequence

1. Electron creates a frameless window showing a loading screen (`shell.html`)
2. `uv sync` runs using the bundled `uv` binary and the packaged `pyproject.toml` + lockfile
3. Electron finds an available port and spawns: `uv run minds --format jsonl --log-file <path> forward --host 127.0.0.1 --port <port> --no-browser`
4. The backend emits a JSONL event `{"event": "login_url", "login_url": "..."}` on stdout
5. Electron waits for the port to accept TCP connections, then navigates directly to the login URL
6. Auth completes (one-time code consumed, session cookie set), the custom title bar is injected, user sees the web UI

### Shutdown

Closing an individual window just tears down that window's views -- the backend keeps running while any window is open. When the last window closes (or the user issues `Cmd+Q` / `Ctrl+Q`), Electron sends SIGTERM to the backend process and waits up to 5 seconds. If the process doesn't exit, SIGKILL is sent.

### Crash recovery

If the backend exits unexpectedly, every open window switches to the error screen (chrome view expanded to fill the window, content/sidebar/requests-panel views torn down) with the last lines from the log file. Clicking "Retry" from any window restarts the backend once; on success every window reloads to its pre-error URL.

### Keyboard shortcuts

- **Open DevTools**: `Ctrl+Shift+C` (Windows/Linux) or `Cmd+Option+I` (macOS)
- **New Window**: `Ctrl+N` / `Cmd+N` -- opens a fresh window on the home page. Also available on macOS via `File > New Window` and the dock icon's right-click menu.
- **Close Window**: `Ctrl+W` / `Cmd+W` -- closes the focused window; the backend keeps running until the last window closes.
- **Quit**: `Ctrl+Q` / `Cmd+Q` -- closes every window and shuts the backend down.

### Multi-window behavior

Each workspace (`/forwarding/{agent-id}/...`) can live in its own window. Uniqueness is enforced across the app: at most one window per workspace.

- **Open in a new window** (from the sidebar): right-click a workspace entry for a native `Open in new window` context menu, or click the hover-revealed icon on the right of the row. Both are suppressed on the entry matching the window's current workspace.
- **Open a blank window**: cmd+N / ctrl+N, `File > New Window`, or the macOS dock menu. Opens a window on the backend's home page (`/`).
- **Plain sidebar click**: navigates the current window to that workspace -- unless some other window is already on it, in which case that window is focused and the sender is untouched.
- **Notifications** pointing at `/forwarding/{X}/...` focus the existing window for workspace `X`, or open a new one. Non-workspace notification URLs and `auth_required` events navigate the most-recently-focused window.
- **Session restore**: on quit, every open window's content URL is recorded to `~/.<MINDS_ROOT_NAME>/window-state.json`. On next launch (after the backend is ready) one window is reopened per recorded URL. URLs pointing at workspaces that no longer exist are silently dropped.

### Environment variables

- `MINDS_HIDE_MENU=1`: Hides the application menu bar (macOS only; Linux/Windows frameless windows have no menu bar).
- `MINDS_ROOT_NAME`: Controls the data-dir/prefix scheme described above (default: `minds`). Must match `[a-z0-9_-]+`.

## Output and logging conventions

The CLI separates two channels, following the same conventions as mngr:

- **stdout**: Command output in the format specified by `--format` (human, json, or jsonl). Machine consumers like the Electron shell use `--format jsonl` to parse structured events.
- **stderr**: Diagnostic logging, always human-readable colored text. Controlled by `-v` (DEBUG), `-vv` (TRACE), and `-q` (suppress).
- **File logging**: `--log-file <path>` adds a persistent JSONL event log using the same envelope format as mngr.

## Bundled binaries

The desktop app bundles platform-specific binaries so users need zero prerequisites:

- **uv**: Downloads Python, creates venvs, installs packages. Downloaded from GitHub releases during `pnpm build`.
- **git**: Required for agent creation (cloning repos). Currently copied from the build machine; a statically-linked distribution should be used for production.

Both are placed in the `resources/` directory (outside the asar archive) and added to `PATH` in the child process environment.

## Data directory

All desktop app state lives in `~/.<MINDS_ROOT_NAME>/` (default: `~/.minds/`):

```
~/.minds/
  .venv/                  # uv-managed Python virtual environment
  .uv-cache/              # uv package cache
  .uv-python/             # uv-managed Python installations
  logs/
    minds.log             # Combined stdout/stderr log from the backend
    minds-events.jsonl    # Structured JSONL event log
  auth/                   # Cookie signing key, one-time codes
  config.toml             # Optional minds config (cloudflare/supertokens URLs)
  window-state.json       # Per-window content URLs, restored on next launch
  mngr/                   # mngr host directory (MNGR_HOST_DIR)
    agents/               # per-agent state managed by mngr
  <agent-id>/             # Per-agent workspace directories
```

`MINDS_ROOT_NAME` is a single env var that isolates an installed minds
from a dev copy. Exporting `MINDS_ROOT_NAME=devminds` moves the entire
layout to `~/.devminds/` (separate venv, caches, logs, auth, agents).
The corresponding `MNGR_HOST_DIR` becomes `~/.devminds/mngr/` and
`MNGR_PREFIX` becomes `devminds-` so tmux sessions and containers for
the two copies never collide. Standalone `mngr` invocations ignore
`MINDS_ROOT_NAME`.

### Configuration file

`~/.<MINDS_ROOT_NAME>/config.toml` is optional. When present, it may set:

```toml
remote_service_connector_url = "https://..."
```

The `REMOTE_SERVICE_CONNECTOR_URL` environment variable overrides the file.
The field has a built-in default that points at the current dev-deployed
server, so packaged minds works out of the box with no config file. The
SuperTokens core URI and API key are configured on the backend server
(alongside the Cloudflare credentials) and never need to be set on the
client.

## Development

### Prerequisites

- Node.js >= 20
- pnpm >= 10
- Python >= 3.11, uv, git (for the Python backend)

### Running locally

```bash
cd apps/minds
pnpm install        # Install Electron and ToDesktop CLI
pnpm start          # Launch the Electron app in dev mode
```

In dev mode, the Electron app skips `uv sync` and uses the monorepo's workspace venv directly (via `uv run --package minds` from the repo root). This means all mngr plugins (claude, modal, etc.) are available without any extra setup, and changes to the Python code are picked up immediately on restart.

### Building for distribution

```bash
pnpm build                        # Prepare resources
pnpm exec todesktop build         # Upload to ToDesktop for native builds
```

ToDesktop builds native installers (.dmg for macOS, .AppImage for Linux), handles code signing, notarization, and auto-update infrastructure.

The build script (`scripts/build.js`) strips the `[tool.uv.sources]` section from the standalone pyproject.toml when copying it to resources, so the packaged app resolves the `minds` package from PyPI instead of a local path.

### Updating the Python package

1. Bump the version pin in `electron/pyproject/pyproject.toml`
2. Regenerate the lockfile: `uv lock --project electron/pyproject`
3. Run `pnpm exec todesktop build` to publish

The new lockfile is shipped in the app bundle. On next launch, `uv sync` installs the updated packages.

## File structure

```
apps/minds/
  package.json              # pnpm + Electron + ToDesktop config
  todesktop.json            # ToDesktop build settings
  electron/
    main.js                 # Electron main process entry point
    preload.js              # Context bridge for renderer IPC
    paths.js                # Platform-aware path resolution
    env-setup.js            # uv sync runner with progress reporting
    backend.js              # Python backend process manager
    shell.html              # Loading and error screens (title bar is injected at runtime)
    assets/
      icon.svg              # App icon (SVG source)
      icon.png              # App icon (PNG for Electron)
    pyproject/
      pyproject.toml        # Standalone: declares minds dependency
      uv.lock               # Pinned lockfile for reproducible installs
  scripts/
    build.js                # Downloads uv/git, copies pyproject to resources/
  resources/                # (gitignored) Built artifacts for packaging
```
