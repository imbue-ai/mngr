# Desktop App

Minds ships as a standalone desktop application built with Electron and distributed via [ToDesktop](https://www.todesktop.com/). The desktop app wraps the existing Python backend -- no code changes are needed to the web UI or agent system.

## How it works

The Electron shell is deliberately thin. It handles four things:

1. **Environment setup**: Runs `uv sync` on launch to install/update the Python environment
2. **Backend lifecycle**: Spawns and monitors the `minds run` process
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
3. Electron finds an available port and spawns: `uv run minds -v --format jsonl --log-file <path> run --host 127.0.0.1 --port <port> --no-browser --config-file <path>` (the packaged build always passes `--config-file` from the bundled `client.toml`)
4. The backend emits a JSONL event `{"event": "login_url", "login_url": "..."}` on stdout
5. Electron waits for the port to accept TCP connections, then navigates directly to the login URL
6. Auth completes (one-time code consumed, session cookie set), the custom title bar is injected, user sees the web UI

### Shutdown

Closing an individual window just tears down that window's views -- the backend keeps running while any window is open. When the last window closes (or the user issues `Cmd+Q` / `Ctrl+Q`), Electron sends SIGTERM to the backend process and waits up to 5 seconds. If the process doesn't exit, SIGKILL is sent.

#### Quitting page

Backend teardown (and, when applicable, stopping running local minds) takes a moment, during which the UI would otherwise sit there looking frozen. To make the state obvious, once a quit is *committed* every open window flips to a full-window "quitting" screen: the same animated wordmark as the startup loading screen (`shell.html`, loaded with a `#quitting` hash so it reveals a status line), with the chrome view expanded to fill the window (content/sidebar/modal views collapse to zero, the same takeover `updateBundleBounds` uses for the loading and error screens). Progress text -- `Quitting…`, `Stopping N minds…`, `Closing…` -- is pushed to it through the existing `status-update` IPC channel.

The flip happens *after* the mind shutdown prompt below (it is gated on the same `isShuttingDown` commit), so cancelling that prompt leaves the app fully intact with no visual change. Headless quits (SIGTERM / SIGINT) skip the flip -- they have no interactive UI to update.

#### Mind shutdown prompt

Agent containers run independently of the backend, so quitting the app would otherwise leave any **shutdown-capable** minds (those on a provider whose host minds can stop/start -- the local `docker` / `lima` backends today; the single `provider_backend_supports_shutdown` predicate is the one place that gate lives) running and consuming machine resources. Before tearing the backend down, Electron asks the backend which such minds are still running (`GET /api/minds/running`, which reads each mind's container state straight from the discovery snapshot the single discovery observer keeps fresh -- the same `host.state` that drives the landing-page Start/Stop controls -- so the dialog appears instantly without shelling out). When the last window is closed via the macOS close button, the close is intercepted so this prompt appears *before* the window disappears. If the running-minds check itself fails, the user is asked to **Quit anyway** or **Cancel** rather than silently quitting. If any minds are running:

- A dialog lists how many and which minds are running, with three choices: **Cancel** (stay open), **Leave running** (quit now; containers keep running), or **Shut down all**. This prompt runs *first*, before any window flips to the quitting page; **Cancel** leaves the app untouched.
- **Leave running** and **Shut down all** both commit the quit, flipping every window to the quitting page (above).
- **Shut down all** stops all the running minds with a single synchronous `POST /api/minds/stop-hosts` (the ids passed as repeated `agent_id` query params), which runs one `mngr stop <ids…> --stop-host` server-side -- mngr stops every named host concurrently via its own executor, so it is one subprocess, not one per mind. Progress shows *in-page on the quitting screen* (`Stopping N minds…`). The endpoint returns the minds still running after the attempt; if any remain (or the request failed), it offers **Retry** / **Quit anyway** / **Cancel quit** via a native dialog (choosing **Cancel quit** reverses the flip and returns the app to its normal running state). Once every mind is down it also stops this env's mngr docker **state container** (`<MNGR_PREFIX>docker-state-<user_id>`, the provider's bookkeeping container that `mngr stop --stop-host` leaves running) via `POST /api/minds/stop-state-container`, so no minds-related container is left running. The state container is stopped, not removed -- its volume (host records) is preserved and it restarts on next use. Only this env's prefix is targeted, so a differently-prefixed state container (e.g. your own `mngr-` docker usage) is never touched.

Programmatic shutdowns (SIGTERM / SIGINT, e.g. `just minds-stop`) skip the prompt and shut down directly. Minds on providers that don't support host shutdown are never counted or stopped -- they don't use local resources.

### Crash recovery

If the backend exits unexpectedly, every open window switches to the error screen (chrome view expanded to fill the window, content/sidebar/modal views torn down) with the last lines from the log file. Clicking "Retry" from any window restarts the backend once; on success every window reloads to its pre-error URL.

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
- **Session restore**: on quit, every open window's content URL is recorded to `~/.<MINDS_ROOT_NAME>/window-state.json` (as `{ windows: [{ url, x, y, width, height, displayId }, ...] }`). On next launch (after the backend is ready) one window is reopened per recorded URL, and each window's titlebar accent is re-derived from that restored URL (see below) -- the accent is not separately persisted. URLs pointing at workspaces that no longer exist are silently dropped. (Older files that still carry a per-window `lastWorkspaceAgentId` field are accepted and the field ignored.)

### Titlebar accent and the neutral chrome

The full-width titlebar (and the thin shell around the content view) adopt the active workspace's accent color while you're on a workspace-scoped screen, and fall back to a **neutral** chrome on every other minds screen. The neutral chrome background comes from the `--titlebar-bg` fallback in `Chrome.jinja` (`var(--c-surface-primary)`: white in light mode, black in dark); its foreground is not a stored value but is derived from the background in pure CSS by the `.titlebar-surface` recipe in `app.css` (an `lch(from …)` relative-color contrast), the same recipe that re-bases the foreground tokens under an active workspace accent. The same neutral surface is used by the startup/quitting/error loading screen (`shell.html`). Workspace accent swatches deliberately exclude pure black and white so a workspace's color can never collide with this neutral chrome (users can still type either into the settings hex input).

The accent is a **pure function of the window's current screen**, not a remembered value. The titlebar is its own `WebContentsView` and can't read the content URL, so the main process derives the accent source from each content navigation (`parseAccentSourceAgentId`: the workspace id on the workspace itself plus its settings / sharing / destroying / recovery screens, `null` on a general screen) and pushes it to the titlebar over a single `accent-changed` IPC; the chrome renderer applies it unconditionally. Main also re-pushes the current value whenever a chrome view (re)loads (via `primeViewWithCachedChromeState`), which covers cold start, new windows, and crash-recovery rebuilds. The narrower "which workspace is actually being *displayed*" signal (`current-workspace-changed`) is separate and drives only the OS window title and the recovery-page auto-redirect. Browser mode derives the same accent directly from the iframe URL in its poll loop.

### Environment variables

- `MINDS_HIDE_MENU=1`: Hides the application menu bar (macOS only; Linux/Windows frameless windows have no menu bar).
- `MINDS_ROOT_NAME`: Selects the data root for the running backend. Default `minds` (i.e. production at `~/.minds/`). Must match `minds(-<env-name>)?`. Activated by `minds env activate <name>`; legacy values like `devminds` are silently treated as unset with a warning.
- `MINDS_CLIENT_CONFIG_PATH`: Path to the per-env `client.toml` the backend should load. Set by `minds env activate`; passing `--config-file` to `minds run` overrides it. The backend refuses to start when neither is set.

## Output and logging conventions

The CLI separates two channels, following the same conventions as mngr:

- **stdout**: Command output in the format specified by `--format` (human, json, or jsonl). Machine consumers like the Electron shell use `--format jsonl` to parse structured events.
- **stderr**: Diagnostic logging, always human-readable colored text. Controlled by `-v` (DEBUG), `-vv` (TRACE), and `-q` (suppress).
- **File logging**: `--log-file <path>` adds a persistent JSONL event log using the same envelope format as mngr.

## Bundled binaries

The desktop app bundles platform-specific binaries so users need zero prerequisites:

- **uv**: Downloads Python, creates venvs, installs packages. Downloaded from GitHub releases during `pnpm build`.
- **git**: Required for agent creation (cloning repos). Currently copied from the build machine; a statically-linked distribution should be used for production.
- **lima**: Required for the Lima launch mode (running agents in Linux VMs). Downloaded from GitHub releases during `pnpm build`. Self-contained on macOS Apple Silicon via Lima's `vz` backend; macOS Intel and Linux still require QEMU on the host machine.

All three are placed in the `resources/` directory (outside the asar archive) and added to `PATH` in the child process environment.

## Data directory

Every minds env owns one data root. Production lives at `~/.minds/`;
every other env lives at `~/.minds-<env-name>/`. The contents are the
same shape:

```
~/.minds-<env-name>/
  .venv/                  # uv-managed Python virtual environment
  .uv-cache/              # uv package cache
  .uv-python/             # uv-managed Python installations
  logs/
    minds.log             # Combined stdout/stderr log from the backend
    minds-events.jsonl    # Structured JSONL event log
  auth/                   # Cookie signing key, one-time codes
  config.toml             # Optional minds user preferences (default account, etc.)
  client.toml             # Per-env public config (URLs only; dev envs only -- staging/production source from in-repo)
  secrets.toml            # Per-env chmod-0600 secrets (Neon DSN, SuperTokens API key; dev envs only)
  window-state.json       # Per-window content URLs + bounds, restored on next launch
  mngr/                   # mngr host directory (MNGR_HOST_DIR)
    agents/               # per-agent state managed by mngr
  <agent-id>/             # Per-agent workspace directories
```

`MINDS_ROOT_NAME` selects which data root the backend uses. Activation
(`minds env activate <name>`) sets it to `minds-<env-name>` (or just
`minds` for production) and exports the derived `MNGR_HOST_DIR` /
`MNGR_PREFIX` / `MINDS_CLIENT_CONFIG_PATH` alongside. Two envs
activated in parallel shells (or by two Electron instances pointed at
two different bundled configs) never share state. Standalone `mngr`
invocations ignore `MINDS_ROOT_NAME`.

### Environment selection

The desktop client picks the env it talks to via shell activation:

```bash
eval "$(uv run minds env activate <name>)"
minds run                                  # or `just minds-start`
```

`minds run` reads `MINDS_CLIENT_CONFIG_PATH` (set by activation) for
the per-env `client.toml`. Passing `--config-file <path>` overrides
the env var. There is no implicit fallback: the backend refuses to
start when neither is set.

The packaged Electron app embeds a `client.toml` + `MINDS_ROOT_NAME`
pair at build time via `MINDS_CLIENT_CONFIG_BUNDLE` and
`MINDS_ROOT_NAME_BUNDLE`, and the Electron startup exports the env
vars + passes `--config-file` explicitly -- end users never have to
activate anything. See `apps/minds/docs/environments.md` for the full
operator workflow and `apps/minds/docs/vault-setup.md` for how
deploy-time secrets flow through HCP Vault.

### Configuration file

`~/.<root>/config.toml` is optional and holds user-personal
preferences only (the default account for new workspaces, the
auto-open behavior for the inbox). It carries no tier-bound
URL -- env selection happens via `MINDS_CLIENT_CONFIG_PATH` /
`--config-file` as described above.

## Development

### Prerequisites

- Node.js 24.15.0 (pinned via `.nvmrc` and `engines.node`)
- pnpm 10.33.4 (pinned via `engines.pnpm`)
- Python 3.12, uv, git (for the Python backend)

`apps/minds/.npmrc` sets `engine-strict=true`, so `pnpm install` refuses to run on any other Node or pnpm version instead of silently producing a broken install.

### Installing the pinned toolchain

The pins are exact patches (`24.15.0`, `10.33.4`) and `engine-strict=true` will reject anything else. Use the recipes below -- they're the paths that reliably hit the exact versions on any given day.

**Node.js 24.15.0** -- via a version manager:

```bash
# nvm (https://github.com/nvm-sh/nvm)
nvm install         # reads apps/minds/.nvmrc
nvm use             # also reads .nvmrc

# fnm (https://github.com/Schniz/fnm)
fnm install         # reads .nvmrc
fnm use             # reads .nvmrc
```

Run `node --version` from inside `apps/minds/` -- it must print `v24.15.0`.

**pnpm 10.33.4** -- via npm:

```bash
npm install --global pnpm@10.33.4
```

Run `pnpm --version` -- it must print `10.33.4`. To swap back to a newer pnpm after working on minds: `npm install --global pnpm@latest`.

**A note on Homebrew**: `brew install node@24` and `brew install pnpm@10` work *if* the kegs currently happen to point at `24.15.0` / `10.33.4`, but Homebrew's `@<major>` formulae move forward through patch releases and there's no clean way to ask for an exact historical patch. Once a keg drifts past the pin, `engine-strict` will reject `pnpm install` and you'll need to switch to the version-manager / npm paths above anyway. If you already have these installed via brew and they still match, great -- just verify with `node --version` / `pnpm --version` before running `pnpm install`.

### Dependency cooldown (minimum release age)

Both package managers are configured to refuse any distribution published less than **14 days** ago, so a freshly-compromised release cannot be pulled into a build (or an end-user install) before it has had time to be noticed and yanked. This applies to transitive dependencies too.

- **JS (pnpm)**: `minimumReleaseAge: 20160` (minutes) in `apps/minds/pnpm-workspace.yaml`. Requires pnpm >= 10.16.0 (we pin 10.33.4).
- **Python (uv)**: `exclude-newer = "14 days"` under `[tool.uv]` in `apps/minds/electron/pyproject/pyproject.toml` (the packaged end-user app).

The cooldown only bites during **resolution** -- `pnpm install` without `--frozen-lockfile`, `pnpm add`/`update`, and `uv lock`/`uv add` or a re-resolve. Frozen installs (CI's `pnpm install --frozen-lockfile`, and `uv sync` replaying an up-to-date lockfile) replay the committed lockfile and are unaffected. If you add or update a dependency and pnpm/uv refuses a version that is too new, either wait out the window or, for pnpm, add a targeted exception via `minimumReleaseAgeExclude`.

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

ToDesktop builds the macOS arm64 native installer (.zip / .dmg), handles code signing, notarization, and auto-update infrastructure. Linux + Windows targets are not currently wired up: `todesktop.js` ships only a `mac:` block, and the release pipeline (`minds-launch-to-msg.yml`) builds and verifies macOS only. The host scripts (`download-binaries.js`, `build.js`) have skeletons for Linux x86_64 and a few Linux native modules ship prebuilds via pnpm, but a packaged Linux install would still need a `linux:` ToDesktop block and a properly bundled git layout (the current `cp $(which git)` skips `libexec/git-core/`).

The build script (`scripts/build.js`) builds a wheel for every workspace package into `resources/wheels/`, rewrites `[tool.uv.sources]` in the staged `resources/pyproject/pyproject.toml` to point each workspace package at its bundled wheel, then runs `uv lock` in-place to regenerate `resources/pyproject/uv.lock` against the rewritten pyproject. The regenerated lockfile is what ships in the app bundle; the dev-time `electron/pyproject/uv.lock` is not committed.

### Updating the Python package

All workspace packages must be listed as direct dependencies in `electron/pyproject/pyproject.toml` — uv ignores `[tool.uv.sources]` path overrides for transitive-only packages and will silently fall back to stale PyPI versions. Keep the dependencies list in sync with `WORKSPACE_PACKAGES` in `scripts/build.js`.

To ship a change:

1. Edit the Python source in the monorepo as usual
2. If adding a new workspace package, add it to both `electron/pyproject/pyproject.toml` (as a direct dep + `[tool.uv.sources]` entry) and `WORKSPACE_PACKAGES` in `scripts/build.js`
3. Run `pnpm exec todesktop build` to publish — `build.js` rebuilds all wheels and regenerates the lockfile automatically

## File structure

```
apps/minds/
  package.json              # pnpm + Electron + ToDesktop config
  todesktop.js              # ToDesktop build settings
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
    build.js                # Downloads uv/git/lima, copies pyproject to resources/
  resources/                # (gitignored) Built artifacts for packaging
```
