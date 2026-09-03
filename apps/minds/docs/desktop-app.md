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

Each window is a frameless `BrowserWindow` (`frame: false` on Linux/Windows, `titleBarStyle: 'hiddenInset'` with `trafficLightPosition` on macOS) hosting ONE web context: the backend-served Mithril SPA shell (`apps/minds/frontend/`). That single page owns the titlebar, client-side routing among the hub pages (the titlebar never reloads), the sandboxed cross-origin iframe that displays workspace content (`WorkspaceFrame`), and the in-DOM Mithril modals (workspace switcher, inbox, help, sign-in, settings, accounts, workspace options). The identical page runs in a plain browser against a local `minds run` -- the desktop app adds only a slim native bridge (`window.mindsNative` from `preload.js`: window controls, native file picker, shell events, the release-channel and update-status calls behind Settings > Updates, and the startup/error/quitting screens).

Workspace content is entered through the minds `/forward-bridge` route, which hands the browser a `mngr forward` plugin session before landing on the plugin's `/goto/<host-id>/` workspace entry; the plugin appends a `frame-ancestors` policy to every workspace response so only the minds chrome (and the workspace's own origin family) may embed it. Chrome<->workspace messaging flows exclusively through the embed contract (see [embed-contract.md](./embed-contract.md)).

A separate `shell.html` page handles the loading spinner, the quitting screen, and the error screen during startup/teardown.

### Startup sequence

1. Electron creates a frameless window showing a loading screen (`shell.html`)
2. `uv sync` runs using the bundled `uv` binary and the packaged `pyproject.toml` + lockfile
3. Electron finds an available port and spawns: `uv run minds -v --format jsonl --log-file <path> run --host 127.0.0.1 --port <port> --no-browser --config-file <path>` (the packaged build always passes `--config-file` from the bundled `client.toml`)
4. The backend emits a JSONL event `{"event": "login_url", "login_url": "..."}` on stdout
5. Electron waits for the port to accept TCP connections, then navigates directly to the login URL
6. Auth completes (one-time code consumed, session cookie set), the custom title bar is injected, user sees the web UI

### Shutdown

Closing an individual window just tears down that window's views -- the backend keeps running while any window is open. **On macOS, closing the last window does not quit the app**: it keeps running with no windows (the dock icon stays), matching standard macOS apps. Re-open a window by clicking the dock icon (or `Cmd+N`), and quit explicitly with `Cmd+Q`. On Windows/Linux the last window's close quits, per those platforms' convention.

A re-opened window always shows the app's *current* state, not just the home page: the backend's home page when it is serving, the loading screen while it is still coming up, and the error screen -- carrying the **Retry** button that restarts the backend -- when startup failed or the backend died while nothing was open. Every entry point that asks for a window shares this: `activate`, `Cmd+N`, `File > New Window`, the dock menu, launching the app again, and a `minds://` deeplink arriving with nothing open. This matters because a windowless app has no other way back: if a request could resolve to no window, the app would sit in the dock unusable until `Cmd+Q`. Closing the window *during* startup is likewise not a cancellation -- the backend finishes coming up and authenticates, and the launch's landing (session restore, or the welcome / consent screens) is still owed: it is applied to the next window you open rather than opening windows unprompted, and is recomputed at that moment, so a session restored long afterwards reflects the machines that exist then. When a quit is *committed* (`Cmd+Q` / `Ctrl+Q`, a SIGTERM/SIGINT, or the last window closing off macOS), Electron sends SIGTERM to the backend process and waits up to 5 seconds. If the process doesn't exit, SIGKILL is sent.

#### Quitting page

Backend teardown (and, when applicable, stopping running local minds) takes a moment, during which the UI would otherwise sit there looking frozen. To make the state obvious, once a quit is *committed* every open window flips to a full-window "quitting" screen: the same animated wordmark as the startup loading screen (`shell.html`, loaded with a `#quitting` hash so it reveals a status line), taking over the whole window. Progress text -- `Quitting…`, `Stopping N minds…`, `Closing…` -- is pushed to it through the existing `status-update` IPC channel.

The flip happens *after* the mind shutdown prompt below (it is gated on the same `isShuttingDown` commit), so cancelling that prompt leaves the app fully intact with no visual change. Headless quits (SIGTERM / SIGINT) skip the flip -- they have no interactive UI to update.

#### Mind shutdown prompt

Agent containers run independently of the backend, so quitting the app would otherwise leave any **local** minds (those on the `docker` / `lima` backends; the single `provider_backend_is_local` predicate is the one place that gate lives) running and consuming the user's own machine. Cloud minds (`aws` / `gcp` / `azure` / `imbue_cloud`) are deliberately out of scope even though they *are* shutdown-capable: they keep running their agents with the app closed, which is the point of running one, so they are stopped from their own Start/Stop control instead of at quit. Before tearing the backend down, Electron asks the backend which local minds are still running (`GET /api/minds/running`, which reads each mind's container state straight from the discovery snapshot the single discovery observer keeps fresh -- the same `host.state` that drives the landing-page Start/Stop controls -- so the dialog appears instantly without shelling out). This prompt is tied to an actual quit, not to closing windows. On macOS, closing windows never quits (the app keeps running with no windows), so the prompt appears only on an explicit `Cmd+Q` / menu Quit. On Windows/Linux, closing the last window *is* a quit, so that window's close button is intercepted and the prompt appears *before* the window disappears. If the running-minds check itself fails, the user is asked to **Quit anyway** or **Cancel** rather than silently quitting. If any minds are running:

- A dialog lists how many and which minds are running, with three choices: **Cancel** (stay open), **Leave running** (quit now; containers keep running), or **Shut down all**. This prompt runs *first*, before any window flips to the quitting page; **Cancel** leaves the app untouched.
- **Leave running** and **Shut down all** both commit the quit, flipping every window to the quitting page (above).
- **Shut down all** stops all the running minds with a single synchronous `POST /api/minds/stop-hosts` (the ids passed as repeated `agent_id` query params), which runs one `mngr stop <ids…> --stop-host` server-side -- mngr stops every named host concurrently via its own executor, so it is one subprocess, not one per mind. Progress shows *in-page on the quitting screen* (`Stopping N minds…`). The endpoint returns the minds still running after the attempt; if any remain (or the request failed), it offers **Retry** / **Quit anyway** / **Cancel quit** via a native dialog (choosing **Cancel quit** reverses the flip and returns the app to its normal running state). Once every mind is down it also stops this env's mngr docker **state container** (`<MNGR_PREFIX>docker-state-<user_id>`, the provider's bookkeeping container that `mngr stop --stop-host` leaves running) via `POST /api/minds/stop-state-container`, so no minds-related container is left running. The state container is stopped, not removed -- its volume (host records) is preserved and it restarts on next use. Only this env's prefix is targeted, so a differently-prefixed state container (e.g. your own `mngr-` docker usage) is never touched.

Programmatic shutdowns (SIGTERM / SIGINT, e.g. `just minds-stop`) skip the prompt and shut down directly. Minds that are not local are never counted or stopped -- they don't use the user's machine.

### Crash recovery

If the backend exits unexpectedly, every open window switches to the error screen (`shell.html` taking over the whole window) with the last lines from the log file. Clicking "Retry" from any window restarts the backend once; on success every window reloads to its pre-error URL.

### Keyboard shortcuts

- **Open DevTools**: `Ctrl+Shift+C` (Windows/Linux) or `Cmd+Option+I` (macOS)
- **New Window**: `Ctrl+N` / `Cmd+N` -- opens a fresh window on the home page. Also available on macOS via `File > New Window` and the dock icon's right-click menu.
- **Close Window**: `Ctrl+W` / `Cmd+W` -- closes the focused window. On macOS the app (and backend) keep running even after the last window closes -- re-open from the dock icon or `Cmd+N`. On Windows/Linux the backend shuts down when the last window closes.
- **Quit**: `Ctrl+Q` / `Cmd+Q` -- closes every window and shuts the backend down. On macOS this is the only way to quit (closing windows does not).

### Multi-window behavior

Each workspace can live in its own window. There is deliberately NO cross-window uniqueness or locking: a window shows whatever it shows, and two windows may display the same workspace (matching the web world, where a user can always open the same page in two tabs).

- **Open in a new window** (from the workspace switcher): right-click a workspace entry for an `Open in new window` context-menu entry (desktop only), or click the arrow icon on the row.
- **Open a blank window**: cmd+N / ctrl+N, `File > New Window`, or the macOS dock menu. Opens a window on the backend's home page (`/`).
- **Plain sidebar click**: always navigates the clicking window to that workspace.
- **Notifications** for workspace `X` (a workspace-origin URL) focus the most-recently-focused window already showing `X` without renavigating it; otherwise they navigate the most-recently-focused window (a new window is never auto-opened). A notification carrying the SPA's `/workspace/<id>?review=...` deep link (what the notification feed's OS banners send) also prefers a window already showing `X`, but always navigates it so the `?review=` param lands and opens the review popup. Any other notification URL and `auth_required` events navigate the most-recently-focused window.
- **Session restore**: on quit, every open window's content URL is recorded to `~/.<MINDS_ROOT_NAME>/window-state.json` (as `{ windows: [{ url, x, y, width, height, displayId }, ...] }`). On next launch (after the backend is ready) one window is reopened per recorded URL (workspace windows restore through the SPA's `/workspace/<id>` route). URLs pointing at workspaces that no longer exist are silently dropped; older file shapes are accepted.

### Deeplinks (minds://)

The app registers the `minds://` URL scheme. Packaged macOS builds get the OS registration from `appProtocolScheme` in `todesktop.js` (ToDesktop emits the `CFBundleURLTypes` Info.plist entry); `app.setAsDefaultProtocolClient` is also called at every startup, using the dev-mode form (electron binary + app path) under `electron .`. Dev-mode registration is a no-op on macOS -- LaunchServices only honors schemes declared in a bundle's Info.plist -- so to exercise deeplinks against a dev app, pass the URL as an argument instead: `electron . 'minds://create?git_url=...'` (the same code path Windows/Linux cold starts use).

To test real OS-level delivery (browser link clicks, `open 'minds://...'`) against a dev app on macOS, patch the checkout's dev Electron bundle once so LaunchServices knows about it. The bundle id must also be made unique: every worktree's dev Electron ships as `com.github.Electron`, and LaunchServices resolves the scheme's handler by bundle id, so a shared id can route the URL to some other checkout's copy.

```bash
PLIST=apps/minds/node_modules/electron/dist/Electron.app/Contents/Info.plist
plutil -insert CFBundleURLTypes -json '[{"CFBundleURLName":"Minds Deeplink","CFBundleURLSchemes":["minds"]}]' "$PLIST"
plutil -replace CFBundleIdentifier -string com.imbue.minds.dev "$PLIST"
mv apps/minds/node_modules/electron/dist/Electron.app apps/minds/node_modules/electron/dist/Minds.app
printf 'Minds.app/Contents/MacOS/Electron' > apps/minds/node_modules/electron/path.txt
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f apps/minds/node_modules/electron/dist/Minds.app
```

The rename makes the browser's external-protocol prompt say "open the minds link with Minds" instead of naming the handler "Electron": macOS derives the shown app name from the bundle's on-disk name, so plist-level CFBundleDisplayName overrides alone do not change it. `path.txt` is how the `electron` npm launcher finds the binary, so it must track the rename. The prompt itself is browser UI and can't be customized further; packaged builds are already named Minds.app.

Then start the dev app (its `setAsDefaultProtocolClient` call points the scheme at the patched bundle) and click minds:// links while it is running. The patch lives in `node_modules` (wiped on reinstall, never committed), and a link clicked while the dev app is *not* running launches bare Electron without the app code -- keep the dev app running. Packaged builds need none of this.

Every OS delivery channel -- macOS `open-url` events, Windows/Linux second-instance argv, and cold-start argv -- routes to a single `handleDeeplink` in `main.js`, which parses the URL with the pure `electron/deeplink.js` helpers (unit-tested in `test/unit/deeplink.test.js`). The URL's host names the action:

- `minds://create?git_url=<repo>&branch=<ref>` focuses the most recent window and lands the user on the Create from Template stepper -- the numbered walkthrough described under [Create from Template](#create-from-template) -- in one of two shells, chosen by context. **Outside a machine** (home/general screens) it navigates the shell to the full page (`/create/template`). **Already inside a machine** it pops the same stepper as a modal (`/create/template/modal`) over that machine, which is less disruptive than a full-page takeover. The modal hosts the *add* branch in place, targeting the machine they are already in: it drops the machine picker, and its last step simply says to paste the copied `/use-template <repo>` message into that chat. That step stays up until the user acknowledges it with **Done** -- it never dismisses itself, so the instruction cannot vanish before it is read. Choosing **Create a new machine** is a bigger job than a popup should host, so it hands off to the full page (`?start=create`, which skips the now-answered chooser) and closes the modal. `main.js` reads the window's current machine id (from `bundle.currentWorkspaceId`, the shell's internal field) and passes it as `current_machine` to pick the shell and to name the machine. Because the modal is hosted in the shared overlay iframe, every navigation inside it goes through the `window.minds` bridge (`navigateContent` + `closeModal`) -- a plain `window.location` would load the destination inside the modal. `branch` accepts anything the create form's Branch input accepts (branch, tag, or commit); when absent it stays blank -- creation then resolves the linked repo's latest version. A `minds://create` link without a `git_url` navigates the shell to the plain create page. Values must be percent-encoded by the sender.
- `minds://` bare, or any unrecognized or malformed URL, just opens/focuses the app. The browser sign-in flow relies on this: the desktop client passes `--success-redirect-url minds://` to the plugin's `auth login` subcommand, whose sign-in success page then offers an "Open app" link back to the app (a deliberate click, so the browser's open-external-app prompt appears on a user gesture rather than unprompted).

Deeplinks never force a sign-in: `/create` loads regardless of account state and the page's own remote-vs-local flow prompts for sign-in only when needed. A deeplink that arrives before startup navigation has settled (backend still starting, or an error takeover showing) is queued last-writer-wins and applied once startup succeeds. This holds on a genuine first run too: an explicit deeplink wins over the welcome screen, landing the new user directly on the pre-filled create page. Both the content-nav path (`deeplinkTargetPath`, the `/create` literal) and the modal path (`deeplinkModalPath`, the `/create/template/modal` literal) are built from a fixed allowlist plus re-encoded query params; raw deeplink text is never handed to `loadURL` or `openModal`.

### Titlebar accent and the neutral chrome

The full-width titlebar (and the thin shell around the workspace iframe) adopt the active workspace's accent color while you're on a workspace-scoped screen, and fall back to a **neutral** chrome on every other minds screen. The neutral chrome background comes from the SPA shell's `--titlebar-bg` fallback (`var(--c-surface-primary)`: white in light mode, black in dark); its foreground is not a stored value but is derived from the background in pure CSS by the `.titlebar-surface` recipe in `frontend/src/style.css` (an `lch(from …)` relative-color contrast), the same recipe that re-bases the foreground tokens under an active workspace accent. The same neutral surface is used by the startup/quitting/error loading screen (`shell.html`). Workspace accent swatches deliberately exclude pure black and white so a workspace's color can never collide with this neutral chrome (users can still type either into the settings hex input).

The accent is a **pure function of the window's current route**, not a remembered value: the SPA shell derives the accent source from the current route (the workspace id on the workspace itself plus its settings / options / backups / destroying / recovery screens, none on a general screen -- see `frontend/src/views/shell/shell-state.ts`) and paints it from the workspace list the `/ui/ws` channel maintains, so a not-yet-cached accent paints on the next channel update. In the desktop app, `main.js` independently tracks the displayed workspace from committed navigations for the OS window title and session restore. All of this behaves identically in Electron and browser mode.

### Environment variables

- `MINDS_HIDE_MENU=1`: Hides the application menu bar (macOS only; Linux/Windows frameless windows have no menu bar).
- `MINDS_ROOT_NAME`: Selects the data root for the running backend. Default `minds` (i.e. production at `~/.minds/`). Must match `minds(-<env-name>)?`. Activated by `minds-admin env activate <name>`; legacy values like `devminds` are silently treated as unset with a warning.
- `MINDS_CLIENT_CONFIG_PATH`: Path to the per-env `client.toml` the backend should load. Set by `minds-admin env activate`; passing `--config-file` to `minds run` overrides it. The backend refuses to start when neither is set.

## Output and logging conventions

The CLI separates two channels, following the same conventions as mngr:

- **stdout**: Command output in the format specified by `--format` (human, json, or jsonl). Machine consumers like the Electron shell use `--format jsonl` to parse structured events.
- **stderr**: Diagnostic logging, always human-readable colored text. Controlled by `-v` (DEBUG), `-vv` (TRACE), and `-q` (suppress).
- **File logging**: `--log-file <path>` adds a persistent JSONL event log using the same envelope format as mngr.

## Bundled binaries

The desktop app bundles platform-specific binaries so users need zero prerequisites:

- **uv**: Downloads Python, creates venvs, installs packages. Downloaded from GitHub releases during `pnpm build`.
- **git**: Required for agent creation (cloning repos). A pinned, SHA256-verified [dugite-native](https://github.com/desktop/dugite-native) payload -- the relocatable git distribution GitHub Desktop builds for embedding in Electron apps -- downloaded during `pnpm build` per `apps/minds/scripts/git-manifest.json`. It is self-contained: the `git` binary plus its `libexec/git-core/` helpers, `share/git-core/templates/`, a system `etc/gitconfig`, and (on Linux) an `ssl/cacert.pem` CA bundle. Because the payload binaries bake in an empty prefix, the backend child environment must -- and does -- set `GIT_EXEC_PATH`, `GIT_TEMPLATE_DIR`, and `GIT_CONFIG_SYSTEM` (plus `GIT_SSL_CAINFO` on Linux); a bare `PATH` prepend is not sufficient. See [specs/minds-managed-git/concise.md](../../../specs/minds-managed-git/concise.md).
- **lima**: Required for the Lima launch mode (running agents in Linux VMs). SHA256-verified download, pinned to the version in `download-binaries.js`. Self-contained on macOS Apple Silicon via Lima's `vz` backend; macOS Intel and Linux still run the VM itself via host QEMU.
- **restic**: Per-workspace backup repositories. Downloaded from GitHub releases.
- **desync**: Content-defined-chunking client that fetches the pre-baked Lima image. Downloaded from GitHub releases. macOS/Linux only.
- **uv-shims**: macOS only, and the one payload that is generated rather than downloaded. It holds a single `install_name_tool` shim, which runs Apple's real tool from the toolchain under `DEVELOPER_DIR` (the standard Xcode location when that is unset) or from `/Library/Developer/CommandLineTools`, and exits nonzero when neither is present. uv unconditionally execs a bare `install_name_tool` after downloading a managed CPython, to rewrite libpython's Mach-O install name ([astral-sh/uv#14893](https://github.com/astral-sh/uv/issues/14893)); on a Mac with no Xcode Command Line Tools, `/usr/bin/install_name_tool` is the xcselect stub, which asks macOS to offer the developer-tools install and so raises a system modal on first launch. uv reports the shim's nonzero exit as a non-fatal warning and libpython keeps its as-shipped install name, which is inert here: Minds runs `bin/python3.12`, which links libpython statically, and none of the bundled packages link it either.

Each is placed in the `resources/` directory (outside the asar archive). The packaged app prepends the `uv-shims`, `uv`, `git`, `lima`, and `desync` directories to the backend child process's `PATH`, and prepends `uv-shims` to the `uv sync` environment setup's `PATH` as well -- it is the only payload on both, because either spawn can be the one that fetches the managed CPython. `restic` and `desync` are also named by explicit absolute path (`MINDS_RESTIC_BINARY`, `MINDS_DESYNC_BINARY`), so their resolution never depends on `PATH` ordering; `restic` is reached *only* that way, its directory never being on `PATH`.

Dev mode reaches the same pinned binaries: it prepends the `git` and `lima` directories to `PATH` and names `restic`, `desync`, and the latchkey curl by absolute path. `lima` matters because `mngr_lima` resolves `limactl` from `PATH` and enforces only a *minimum* version, so a developer's newer system lima would pass the check and then hang agent creation on the 2.1.x forwarder regression the pin exists to avoid.

`uv` is the deliberate exception. Dev runs the monorepo workspace through `uv run --package minds`, against the same `.venv` and `uv.lock` the developer's shell drives, so it uses *their* uv rather than risking lockfile-format skew against shared state from a second pinned one. It is therefore a bundled binary dev neither downloads nor resolves (`BINARIES[].usedInDev`), and `uv-shims` follows it: dev skips environment setup entirely and shadows nothing for the developer's own uv.

There is deliberately no bundled `qemu-img`. The pre-baked image is published, downloaded, and consumed as a **raw** image end to end, so nothing converts it. See [lima-image.md](./deploy/setup/lima-image.md) for the whole pipeline, and "Why the image is raw" below.

### How the shipped binaries are chosen

`scripts/build.js` (`pnpm build`, the first half of `pnpm dist`) is the only stage whose output reaches the app. It runs on whichever machine invokes `pnpm dist` -- in CI, the arm64 `minds-runner` -- and downloads for its own `process.arch`. ToDesktop then packages the uploaded `resources/` into `Contents/Resources` via `extraResources`, which is what `paths.getResourcesDir()` resolves to (`process.resourcesPath`) in a packaged app.

What `build.js` stages is `scripts/download-binaries.js`'s `BINARIES` table, iterated -- not a list written out a second time. Naming the downloaders individually is what shipped `desync`, and later the latchkey `curl`, staged by nothing that reaches the app.

`extraResources` is the only channel that reaches the shipped app, so `appFiles` excludes `resources/` wholesale (`'!resources/**'`) -- anything it matched would be packed into `app.asar` as a second copy nothing reads.

Two things would put that copy back, so neither is wired:

- **`todesktop:beforeInstall`.** ToDesktop runs a hook script against `app-wrapper/app/`, so anything it downloads is folded into `app.asar`. Its agent is x86_64, so the binaries it fetches are Intel ones inside an arm64 app -- unreachable *and* unrunnable. `scripts/build.js` is the only stage whose output ships.
- **`mac.additionalBinariesToSign`.** The builder's signing preflight rejects a listed path that is missing from the app-files upload, so every entry pins its subtree into that upload. It buys nothing: ToDesktop deep-signs every Mach-O under `Contents/Resources` with `mac.entitlements` whether or not it is listed.

### Why the image is raw

Lima consumes the pre-baked image directly as raw, so the app ships no image-conversion tool.

`limactl` embeds `go-qcow2reader` and a pure-Go `nativeimgutil`. Its `proxyimgutil` prefers the `qemu-img` binary but falls back to the Go implementation when it is absent (`exec.ErrNotFound`), and `EnsureDisk` auto-detects the base disk's format (raw, qcow2, or asif). The `vz` driver's `diskImageFormat` defaults to **raw**, with a `convertRawToRaw` fast path. Verified by booting a Lima VM from a raw base disk with `qemu-img` absent from `PATH`: it reached `READY` with a working guest.

Raw is also what `desync` chunks, so publishing raw means the assembled bytes are the bytes Lima boots -- the manifest's SHA-256 covers exactly the image that runs. An earlier design converted the assembled raw to qcow2, which Lima then converted straight back to raw.

Raw costs no extra disk. On the real 20 GiB image the sparse raw occupies **4.9 GiB** on disk versus **5.1 GiB** for the qcow2: qcow2's L1/L2 and refcount tables, plus its 64 KiB cluster granularity, cost more than the filesystem's 4 KiB-granular holes. Only the apparent size differs (`ls` reports 20 GiB, `du` reports what is allocated), so tools that do not understand sparse files will inflate it.

### macOS Intel (x86_64) is not supported

ToDesktop publishes `arm64`, `x64`, and `universal` mac artifacts, but only arm64 works, and only it is fetched and verified by `.github/workflows/minds-launch-to-msg.yml`. In the published x64 app, `Contents/MacOS/Minds` is x86_64 while the bundled `uv`, `restic`, and `limactl` are arm64, so it cannot launch a VM.

The cause is structural. `build.js` stages binaries for the arch of the machine it runs on, and all three mac artifacts are packaged from that one upload. ToDesktop's own build agent is x86_64, so nothing on the build server can supply arm64 bytes either.

ToDesktop exposes no arch selection -- its config schema has no `mac.target`/`mac.arch`, and the CLI has no `--arch` -- so the x64 and universal artifacts cannot be turned off from this repo. Supporting Intel would need `build.js` to stage both arches (it already downloads per-arch; nothing forces it to fetch only its own) and either a per-arch `extraResources` mapping or `lipo`-merged universal binaries, plus a pre-baked x86_64 Lima image, without which an Intel app's prefetch reports `VERSION_UNAVAILABLE` and builds in-VM anyway. `git` is already universal, since `xcrun --find git` returns Apple's fat binary.

### Updating the bundled git

git tracks upstream security releases, so the pinned dugite-native payload needs periodic bumping. A weekly CI workflow (`.github/workflows/minds-git-freshness.yml`) opens (or updates) a tracking issue when a dugite-native release carrying a **newer upstream git version** has cleared the repo's 14-day dependency cooldown (the same minimum-release-age posture as `pnpm-workspace.yaml` and the packaged pyproject). It deliberately does not nag on same-git-version dugite rebuilds, and ignores releases still inside the cooldown window. To update:

1. Pick the new dugite-native tag from the freshness tracking issue (or, for an urgent CVE, directly -- you may bump before the cooldown window at your discretion; the automated nag waits it out).
2. Update `apps/minds/scripts/git-manifest.json`: the `dugiteNativeTag`, the `gitVersion`, all five asset names (each embeds a dugite-native commit short-SHA, so record them verbatim), and each target's hash taken from the release's `.sha256` companion asset.
3. Independently download each tarball and recompute its SHA256, then compare against the values you just recorded (pinning defends against future substitution, not against copying a wrong value in).
4. CI runs the bundled-git acceptance test on both shipped targets -- linux-x64 via offload and darwin-arm64 via a GitHub-hosted macOS runner (`test-minds-bundled-git-macos` in `ci.yml`) -- so a green PR proves the bump. Run it locally on a mac as well if you touch any of the unshipped manifest targets (darwin-x64, linux-arm64).
5. Ship through the normal release process; the freshness workflow closes the tracking issue on its next run.

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
(`minds-admin env activate <name>`) sets it to `minds-<env-name>` (or just
`minds` for production) and exports the derived `MNGR_HOST_DIR` /
`MNGR_PREFIX` / `MINDS_CLIENT_CONFIG_PATH` alongside. Two envs
activated in parallel shells (or by two Electron instances pointed at
two different bundled configs) never share state. Standalone `mngr`
invocations ignore `MINDS_ROOT_NAME`.

### Environment selection

The desktop client picks the env it talks to via shell activation:

```bash
eval "$(uv run minds-admin env activate <name>)"
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
activate anything. See `apps/minds/docs/deploy/reference/environments.md` for the full
operator workflow and `apps/minds/docs/deploy/setup/vault.md` for how
deploy-time secrets flow through HCP Vault.

### Configuration file

`~/.<root>/config.toml` is optional and holds user-personal
preferences only (the default account for new workspaces, the
error-reporting settings). It carries no tier-bound
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

ToDesktop builds the macOS arm64 native installer (.zip / .dmg), handles code signing, notarization, and auto-update infrastructure. Linux + Windows targets are not currently wired up: `todesktop.js` ships only a `mac:` block, and the release pipeline (`minds-launch-to-msg.yml`) builds and verifies macOS only. The host scripts (`download-binaries.js`, `build.js`) have skeletons for Linux x86_64 and a few Linux native modules ship prebuilds via pnpm; git for Linux is already the complete dugite-native manifest payload (continuously proven by the bundled-git acceptance test on Linux in CI), so the only remaining gap for a packaged Linux install is a `linux:` ToDesktop block.

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
    deeplink.js             # Pure minds:// URL parsing (electron-free, unit-tested)
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
    build.js                # Build orchestrator: downloads binaries, builds wheels, stages resources/
    download-binaries.js    # BINARIES table: pinned, hash-verified downloads (uv, git, restic, desync, lima, curl) + the generated uv-shims
    ensure-binaries.js      # Dev: provisions BINARIES into the shared cache, symlinks resources/ at it
    git-manifest.json       # Pinned dugite-native git payload: tag, version, per-target hashes
  resources/                # (gitignored) Built artifacts for packaging
```
