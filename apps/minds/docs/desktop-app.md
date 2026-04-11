# Desktop App

Minds ships as a standalone desktop application built with Electron and distributed via [ToDesktop](https://www.todesktop.com/). The desktop app wraps the existing Python backend -- no code changes are needed to the web UI or agent system.

## How it works

The Electron shell is deliberately thin. It handles four things:

1. **Environment setup**: Runs `uv sync` on launch to install/update the Python environment
2. **Backend lifecycle**: Spawns and monitors the `mind forward` process
3. **Auth handshake**: Parses the login URL from stdout and navigates to it
4. **Window management**: Displays the backend's web UI in a native window

Everything else -- agent creation, discovery, proxying, authentication, the web UI -- remains in the Python backend, unchanged. See [overview.md](./overview.md) for details on the forwarding server architecture.

### Startup sequence

1. Electron creates a window showing a loading screen
2. `uv sync` runs using the bundled `uv` binary and the packaged `pyproject.toml` + lockfile
3. Electron finds an available port and spawns: `uv run mind --format jsonl --log-file <path> forward --host 127.0.0.1 --port <port> --no-browser`
4. The backend emits a JSONL event `{"event": "login_url", "login_url": "..."}` on stdout
5. Electron waits for the port to accept TCP connections, then navigates to the login URL
6. Auth completes (one-time code consumed, session cookie set), user sees the web UI

### Shutdown

When the user closes the window, Electron sends SIGTERM to the backend process and waits up to 5 seconds. If the process doesn't exit, SIGKILL is sent.

### Crash recovery

If the backend exits unexpectedly, Electron shows an error screen with the last lines from the log file and a "Retry" button that restarts the backend.

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

All desktop app state lives in `~/.minds/`:

```
~/.minds/
  .venv/                  # uv-managed Python virtual environment
  .uv-cache/              # uv package cache
  .uv-python/             # uv-managed Python installations
  logs/
    minds.log             # Combined stdout/stderr log from the backend
    minds-events.jsonl    # Structured JSONL event log
  auth/                   # Cookie signing key, one-time codes
  <agent-id>/             # Per-agent directories
```

## Development

### Prerequisites

- Node.js >= 20
- pnpm >= 10
- Python >= 3.11, uv, git (for the Python backend)

### Running locally

```bash
cd apps/minds
pnpm install        # Install Electron and ToDesktop CLI
pnpm build          # Download uv/git binaries into resources/
pnpm start          # Launch the Electron app in dev mode
```

In dev mode, `paths.js` resolves resources relative to the project directory instead of `process.resourcesPath`. The standalone `electron/pyproject/pyproject.toml` uses an editable path reference to the local minds package.

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
    loading.html            # Loading/setup screen
    error.html              # Error screen with retry
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
