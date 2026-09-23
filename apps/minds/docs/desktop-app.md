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

A separate `shell.html` page -- the *loading document* -- handles the loading screen, the quitting screen, and the error screen during startup/teardown. It is a white page carrying the brand lockup. On an installation's first launch it plays the intro: the lockup resolves out of focus, two lines type in and out beneath it in a serif, and the lockup travels up to its *parked* place (16px tall, centered in the titlebar band). Every later launch opens on that parked state at once. Under the parked mark an indeterminate bar and a status line report which phase startup is in (environment setup, then "Starting Mind..."), and a "Show details" toggle, closed by default, opens the full startup console log (every line Electron forwards, minus the ones carrying a credential; see `electron/startup-log.js`). A click or a keypress during the intro jumps to the parked state. `prefers-reduced-motion` skips the intro entirely.

Electron owns whether the intro has played (`intro-seen.json` in the data root, written when the film starts, so a quit mid-film still counts). The backend separately owns whether *onboarding is complete* -- whether the install has ever started creating a workspace, or signed in from the start flow's "I already have one" answer -- as `is_onboarding_complete` in `config.toml`. The two are independent: the film is about a launch, the flag is about the install.

### Startup sequence

1. Electron creates a frameless window showing the loading document (`shell.html`, loaded with a `#intro` hash on the install's first launch)
2. `uv sync` runs using the bundled `uv` binary and the packaged `pyproject.toml` + lockfile
3. Electron finds an available port and spawns: `uv run minds -v --format jsonl --log-file <path> run --host 127.0.0.1 --port <port> --no-browser --config-file <path>` (the packaged build always passes `--config-file` from the bundled `client.toml`)
4. The backend emits a JSONL event `{"event": "login_url", "login_url": "..."}` on stdout
5. Electron waits for the port to accept TCP connections, consumes the one-time code, and asks `/ui/api/app-status` where to land
6. Once the loading document reports its intro over (an `intro-finished` IPC; immediate when no intro plays), the first route lands: the start flow (`/start`) when onboarding is incomplete and no workspace exists, else the consent screen, the home page, or the restored session (see `electron/startup-routing.js`)

### The first run

The start flow (`/start`) is a chat. Its titlebar holds only the lockup, centered, exactly where the loading document parked it, so the hand-off between documents is a substitution. The page asks "Wait.. what is honest software?" and answers itself in five points, each behind a chevron that opens a short explanation; pressing "Sounds great, let's continue" starts the first workspace's questions, one at a time: where to run it (a two-column comparison of Imbue Cloud and Custom, with "I already have one (log in)" as the quieter way out for a returning user), then, for the cloud, an Imbue account (the existing browser sign-in) and, when that account's email is not yet verified, the click on the emailed link: the flow sends the verification email (signing up sends none of its own), polls the connector's verdict, offers a resend, and only then submits. The cloud answer submits the create form's remote preset directly; Custom opens the create form itself as a modal. Every answer can be taken back with the undo inside its bubble until a create is submitted.

Submitting a create lands on the creation page (`/creating/<attempt-id>`), which wears the normal app frame: the home button, the workspace's name and accent (from its in-flight row in the workspace list), and the permissions / settings / share buttons, which open a "check back here after the workspace is created" dialog until the workspace exists. The conversation continues there: a user turn restating the chosen settings, the agent's "Setting up your workspace" line, reading material for the wait (five chevron toggles, each opening a paragraph and a link out to the product page), and a full-width loading box with the progress bar, the stage line, and the expandable log. When the attempt is ready, "Your workspace is ready! What would you like to do first?" streams with a numbered list of five ways to start, each a title with a line under it under it, the whole conversation so far (the manifesto exchange, the questions and answers, the settings, the setup and ready lines) is handed to the new workspace as its first chat (`POST /ui/api/create/attempts/<attempt-id>/welcome-chat`, which runs the template's `system/scripts/seed_welcome_chat.py` inside the workspace through `mngr exec`), and the *wash* carries the app in: a disc of the workspace's accent grows out of the loading box until it covers the window, the shell enters the workspace under it, and the color lifts. The workspace opens on that chat, with the conversation as its transcript and a composer under it; the user's first message there picks the provider account and starts the workspace's first agent. A workspace that cannot take the chat (its chat app not up, an older template) is entered all the same, on its plain landing page. Later creates from the home page's Create button land on the same creation page: the manifesto exchange opens it, already on the page, followed by the create's own turns.

In plain-browser mode there is no loading document: the start flow begins at the chat, with the mark already in the titlebar, and the home page redirects to `/start` itself while onboarding is incomplete and discovery has found nothing.

### Shutdown

Closing an individual window just tears down that window's views -- the backend keeps running while any window is open. **On macOS, closing the last window does not quit the app**: it keeps running with no windows (the dock icon stays), matching standard macOS apps. Re-open a window by clicking the dock icon (or `Cmd+N`), and quit explicitly with `Cmd+Q`. On Windows/Linux the last window's close quits, per those platforms' convention.

A re-opened window always shows the app's *current* state, not just the home page: the backend's home page when it is serving, the loading screen while it is still coming up, and the error screen -- carrying the **Retry** button that restarts the backend -- when startup failed or the backend died while nothing was open. Every entry point that asks for a window shares this: `activate`, `Cmd+N`, `File > New Window`, the dock menu, launching the app again, and a `minds://` deeplink arriving with nothing open. This matters because a windowless app has no other way back: if a request could resolve to no window, the app would sit in the dock unusable until `Cmd+Q`. Closing the window *during* startup is likewise not a cancellation -- the backend finishes coming up and authenticates, and the launch's landing (session restore, or the start flow / consent screens) is still owed: it is applied to the next window you open rather than opening windows unprompted, and is recomputed at that moment, so a session restored long afterwards reflects the machines that exist then. When a quit is *committed* (`Cmd+Q` / `Ctrl+Q`, a SIGTERM/SIGINT, or the last window closing off macOS), Electron sends SIGTERM to the backend process and waits up to 5 seconds. If the process doesn't exit, SIGKILL is sent.

#### Quitting page

Backend teardown (and, when applicable, stopping running local workspaces) takes a moment, during which the UI would otherwise sit there looking frozen. To make the state obvious, once a quit is *committed* every open window flips to a full-window "quitting" screen: the loading document's parked mark over a status line (`shell.html`, loaded with a `#quitting` hash so it hides the progress bar), taking over the whole window. Progress text -- `Quitting…`, `Stopping N workspaces…`, `Closing…` -- is pushed to it through the existing `status-update` IPC channel.

The flip happens *after* the workspace shutdown prompt below (it is gated on the same `isShuttingDown` commit), so cancelling that prompt leaves the app fully intact with no visual change. Headless quits (SIGTERM / SIGINT) skip the flip -- they have no interactive UI to update.

#### Workspace shutdown prompt

Agent containers run independently of the backend, so quitting the app would otherwise leave any **local** workspaces (those on the `docker` / `lima` backends; the single `provider_backend_is_local` predicate is the one place that gate lives) running and consuming the user's own computer. Cloud workspaces (`aws` / `gcp` / `azure` / `imbue_cloud`) are deliberately out of scope even though they *are* shutdown-capable: they keep running their agents with the app closed, which is the point of running one, so they are stopped from their own Start/Stop control instead of at quit. Before tearing the backend down, Electron asks the backend which local workspaces are still running (`GET /api/minds/running`, which reads each workspace's container state straight from the discovery snapshot the single discovery observer keeps fresh -- the same `host.state` that drives the landing-page Start/Stop controls -- so the dialog appears instantly without shelling out). This prompt is tied to an actual quit, not to closing windows. On macOS, closing windows never quits (the app keeps running with no windows), so the prompt appears only on an explicit `Cmd+Q` / menu Quit. On Windows/Linux, closing the last window *is* a quit, so that window's close button is intercepted and the prompt appears *before* the window disappears. If the running-workspaces check itself fails, the user is asked to **Quit anyway** or **Cancel** rather than silently quitting. If any workspaces are running:

- A dialog lists how many and which workspaces are running, with three choices: **Cancel** (stay open), **Leave running** (quit now; containers keep running), or **Shut down all**. This prompt runs *first*, before any window flips to the quitting page; **Cancel** leaves the app untouched.
- **Leave running** and **Shut down all** both commit the quit, flipping every window to the quitting page (above).
- **Shut down all** stops all the running workspaces with a single synchronous `POST /api/minds/stop-hosts` (the ids passed as repeated `agent_id` query params), which runs one `mngr stop <ids…> --stop-host` server-side -- mngr stops every named host concurrently via its own executor, so it is one subprocess, not one per workspace. Progress shows *in-page on the quitting screen* (`Stopping N workspaces…`). The endpoint returns the workspaces still running after the attempt; if any remain (or the request failed), it offers **Retry** / **Quit anyway** / **Cancel quit** via a native dialog (choosing **Cancel quit** reverses the flip and returns the app to its normal running state). Once every workspace is down it also stops this env's mngr docker **state container** (`<MNGR_PREFIX>docker-state-<user_id>`, the provider's bookkeeping container that `mngr stop --stop-host` leaves running) via `POST /api/minds/stop-state-container`, so no workspace-related container is left running. The state container is stopped, not removed -- its volume (host records) is preserved and it restarts on next use. Only this env's prefix is targeted, so a differently-prefixed state container (e.g. your own `mngr-` docker usage) is never touched.

Programmatic shutdowns (SIGTERM / SIGINT, e.g. `just minds-stop`) skip the prompt and shut down directly. Workspaces that are not local are never counted or stopped -- they don't use the user's computer.

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
- **Notifications** for workspace `X` (a workspace-origin URL) focus the most-recently-focused window already showing `X` without renavigating it; otherwise they navigate the most-recently-focused window (a new window is never auto-opened). A notification carrying one of the SPA's `/workspace/<id>` deep links (what the notification feed's OS banners send: `?review=` for a permission request, `?chat=` for an agent message, `/backups` for a backup event) also prefers a window already showing `X`, but always navigates it so the param or sub-screen lands and opens the review popup, the chat, or the backups page. Any other notification URL (the accounts page for an account-level backup event) and `auth_required` events navigate the most-recently-focused window.
- **Focus relay**: main relays each window's own `focus` and `blur` to its page (`window-focus-changed` over the preload bridge). The page cannot see them itself while keyboard focus sits inside the workspace iframe (Chromium fires the top-level window's focus events only for the chrome document), and the backend's OS-banner gate -- no banner when a focused window is showing the asking workspace -- reads the page's focus report over the `/ui/ws` channel.
- **OS banners and toasts**: every notification is an entry in the backend's feed (permission requests, chat agents' messages, backup events); the backend decides whether an entry becomes a native banner (delivery style, master toggle, the focus gate) and emits it on stdout for main to show, titled with the workspace, subtitled with the headline. In-app toast cards flash in every open window regardless of focus. Nothing reaches the OS outside the desktop app: a bare `minds run` in a browser shows the bell, the feed, and the cards, and no banners. When no app handles a `mailto:` or `tel:` link, the address is copied to the clipboard and the window the click happened in shows an in-app toast saying so (no system notification).
- **First notification choice**: delivery defaults to both in-app cards and system banners. Once a notification arrives, users without saved preferences see a non-blocking chooser in the focused desktop window. They can keep both, use either channel, or keep only the bell. Choosing in the card or in Settings saves the preference for future launches. Existing saved settings are preserved. In-app notification clicks first look for another window already showing the target workspace; if one exists, that window receives focus and the notification destination, leaving the source window where it was.
- **Opening a notification**: native banners, toast cards, and feed rows run the same entry action in the selected window. It dismisses the toast, clears the shared feed entry in every window, closes the notification menu, and opens the destination. A permission request's notification is only a reminder: dismissing it does not answer the request, which stays pending in the workspace's Permissions view until approved or denied. Dismissed reminders stay dismissed after a restart, and a request that was already pending when the app launched rejoins the feed without a system banner (the gateway records when each request was filed). The Permissions button's badge reads the pending-request channel independently of the notification feed, so opening or clearing a reminder leaves that badge visible.
- **Browser sign-in and email verification** raise the app themselves. Both finish in the system browser, which took OS focus with it, so the window that started the flow brings the whole app to the front (stealing focus back, on macOS) the moment its poller sees the sign-in land or the address come back verified. Once per flow -- the pollers run every second or three, and raising on every tick would take focus off the user repeatedly -- and a no-op when that window already has focus.
- **Session restore**: on quit, every open window's content URL is recorded to `~/.<MINDS_ROOT_NAME>/window-state.json` (as `{ windows: [{ url, x, y, width, height, displayId }, ...] }`). On next launch (after the backend is ready) one window is reopened per recorded URL (workspace windows restore through the SPA's `/workspace/<id>` route). URLs pointing at workspaces that no longer exist are silently dropped; older file shapes are accepted.

### Deeplinks (minds://)

The app registers the `minds://` URL scheme. Packaged macOS builds get the OS registration from `appProtocolScheme` in `todesktop.js` (ToDesktop emits the `CFBundleURLTypes` Info.plist entry); `app.setAsDefaultProtocolClient` is also called at every startup, using the dev-mode form (electron binary + app path) under `electron .`. Dev-mode registration is a no-op on macOS -- LaunchServices only honors schemes declared in a bundle's Info.plist -- so to exercise deeplinks against a dev app, pass the URL as an argument instead: `electron . 'minds://create?git_url=...'` (the same code path Windows/Linux cold starts use).

To test real OS-level delivery (browser link clicks, `open 'minds://...'`) against a dev app on macOS, patch the checkout's dev Electron bundle once so LaunchServices knows about it. The bundle id must also be made unique: every worktree's dev Electron ships as `com.github.Electron`, and LaunchServices resolves the scheme's handler by bundle id, so a shared id can route the URL to some other checkout's copy.

```bash
PLIST=apps/minds/node_modules/electron/dist/Electron.app/Contents/Info.plist
plutil -insert CFBundleURLTypes -json '[{"CFBundleURLName":"Mind Deeplink","CFBundleURLSchemes":["minds"]}]' "$PLIST"
plutil -replace CFBundleIdentifier -string com.imbue.minds.dev "$PLIST"
mv apps/minds/node_modules/electron/dist/Electron.app apps/minds/node_modules/electron/dist/Mind.app
printf 'Mind.app/Contents/MacOS/Electron' > apps/minds/node_modules/electron/path.txt
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f apps/minds/node_modules/electron/dist/Mind.app
```

The rename makes the browser's external-protocol prompt say "open the minds link with Mind" instead of naming the handler "Electron": macOS derives the shown app name from the bundle's on-disk name, so plist-level CFBundleDisplayName overrides alone do not change it. `path.txt` is how the `electron` npm launcher finds the binary, so it must track the rename. The prompt itself is browser UI and can't be customized further; packaged builds are already named Mind.app.

Then start the dev app (its `setAsDefaultProtocolClient` call points the scheme at the patched bundle) and click minds:// links while it is running. The patch lives in `node_modules` (wiped on reinstall, never committed), and a link clicked while the dev app is *not* running launches bare Electron without the app code -- keep the dev app running. Packaged builds need none of this.

Every OS delivery channel -- macOS `open-url` events, Windows/Linux second-instance argv, and cold-start argv -- routes to a single `handleDeeplink` in `main.js`, which parses the URL with the pure `electron/deeplink.js` helpers (unit-tested in `test/unit/deeplink.test.js`). The URL's host names the action:

- `minds://create?git_url=<repo>&branch=<ref>` focuses the most recent window and lands the user on the Create from Template stepper -- the numbered walkthrough described under [Create from Template](#create-from-template) -- in one of two shells, chosen by context. **Outside a machine** (home/general screens) it navigates the shell to the full page (`/create/template`). **Already inside a machine** it pops the same stepper as a modal (`/create/template/modal`) over that machine, which is less disruptive than a full-page takeover. The modal hosts the *add* branch in place, targeting the machine they are already in: it drops the machine picker, and its last step simply says to paste the copied `/use-template <repo>` message into that chat. That step stays up until the user acknowledges it with **Done** -- it never dismisses itself, so the instruction cannot vanish before it is read. Choosing **Create a new machine** is a bigger job than a popup should host, so it hands off to the full page (`?start=create`, which skips the now-answered chooser) and closes the modal. `main.js` reads the window's current machine id (from `bundle.currentWorkspaceId`, the shell's internal field) and passes it as `current_machine` to pick the shell and to name the machine. Because the modal is hosted in the shared overlay iframe, every navigation inside it goes through the `window.minds` bridge (`navigateContent` + `closeModal`) -- a plain `window.location` would load the destination inside the modal. `branch` accepts anything the create form's Branch input accepts (branch, tag, or commit); when absent it stays blank -- creation then resolves the linked repo's latest version. A `minds://create` link without a `git_url` navigates the shell to the plain create page. Values must be percent-encoded by the sender.
- `minds://` bare, or any unrecognized or malformed URL, just opens/focuses the app. The browser sign-in flow relies on this: the desktop client passes `--success-redirect-url minds://` to the plugin's `auth login` subcommand, whose sign-in success page then offers an "Open app" link back to the app (a deliberate click, so the browser's open-external-app prompt appears on a user gesture rather than unprompted). The app normally raises itself before that link is needed (see [Multi-window behavior](#multi-window-behavior)); the link is what is left when it cannot -- the app was quit mid-flow, or no window is open.

Deeplinks never force a sign-in: `/create` loads regardless of account state and the page's own remote-vs-local flow prompts for sign-in only when needed. A deeplink that arrives before startup navigation has settled (backend still starting, or an error takeover showing) is queued last-writer-wins and applied once startup succeeds. This holds on a genuine first run too: an explicit deeplink wins over the start flow, landing the new user directly on the pre-filled create page. Both the content-nav path (`deeplinkTargetPath`, the `/create` literal) and the modal path (`deeplinkModalPath`, the `/create/template/modal` literal) are built from a fixed allowlist plus re-encoded query params; raw deeplink text is never handed to `loadURL` or `openModal`.

### Titlebar accent and the neutral chrome

The full-width titlebar (and the thin shell around the workspace iframe) adopt the active workspace's accent color while you're on a workspace-scoped screen, and fall back to a **neutral** chrome on every other minds screen. The neutral chrome background comes from the SPA shell's `--titlebar-bg` fallback (`var(--c-surface-primary)`: white in light mode, black in dark); its foreground is not a stored value but is derived from the background in pure CSS by the `.titlebar-surface` recipe in `frontend/src/style.css` (an `lch(from …)` relative-color contrast), the same recipe that re-bases the foreground tokens under an active workspace accent. The loading document (`shell.html`) is white regardless of theme. Workspace accent swatches deliberately exclude pure black and white so a workspace's color can never collide with this neutral chrome (users can still type either into the settings hex input).

The accent is a **pure function of the window's current route**, not a remembered value: the SPA shell derives the accent source from the current route (the workspace id on the workspace itself plus its settings / options / backups / destroying / recovery screens, none on a general screen -- see `frontend/src/views/shell/shell-state.ts`) and paints it from the workspace list the `/ui/ws` channel maintains, so a not-yet-cached accent paints on the next channel update. In the desktop app, `main.js` independently tracks the displayed workspace from committed navigations for the OS window title and session restore. All of this behaves identically in Electron and browser mode.

### Environment variables

- `MINDS_HIDE_MENU=1`: Hides the application menu bar (macOS only; Linux/Windows frameless windows have no menu bar).
- `MINDS_ROOT_NAME`: Selects the data root for the running backend. Default `minds` (i.e. production at `~/.minds/`). Must match `minds(-<env-name>)?`.
- `MINDS_CLIENT_CONFIG_PATH`: Path to the per-env `client.toml` the backend should load; passing `--config-file` to `minds run` overrides it. When neither is set and `MINDS_ROOT_NAME` is unset (or `minds`), the backend loads the in-repo production `client.toml`. It refuses to start only when `MINDS_ROOT_NAME` names another env and nothing says where that env's config lives.

## Output and logging conventions

The CLI separates two channels, following the same conventions as mngr:

- **stdout**: Command output in the format specified by `--format` (human, json, or jsonl). Machine consumers like the Electron shell use `--format jsonl` to parse structured events.
- **stderr**: Diagnostic logging, always human-readable colored text. Controlled by `-v` (DEBUG), `-vv` (TRACE), and `-q` (suppress).
- **File logging**: `--log-file <path>` adds a persistent JSONL event log using the same envelope format as mngr.

## Bundled binaries

The desktop app bundles platform-specific binaries so users need zero prerequisites:

- **uv**: Downloads Python, creates venvs, installs packages. Downloaded from GitHub releases during `pnpm build`.
- **git**: Required for agent creation (cloning repos). A pinned, SHA256-verified [dugite-native](https://github.com/desktop/dugite-native) payload -- the relocatable git distribution GitHub Desktop builds for embedding in Electron apps -- downloaded during `pnpm build` per `apps/minds/scripts/git-manifest.json`. It is self-contained: the `git` binary plus its `libexec/git-core/` helpers, `share/git-core/templates/`, a system `etc/gitconfig`, and (on Linux) an `ssl/cacert.pem` CA bundle. Because the payload binaries bake in an empty prefix, the backend child environment must -- and does -- set `GIT_EXEC_PATH`, `GIT_TEMPLATE_DIR`, and `GIT_CONFIG_SYSTEM` (plus `GIT_SSL_CAINFO` on Linux); a bare `PATH` prepend is not sufficient. See [specs/minds-managed-git/concise.md](../../../specs/minds-managed-git/concise.md).
- **lima**: Required for the Lima launch mode (running agents in Linux VMs). SHA256-verified download, pinned to the version in `download-binaries.js`. Self-contained on macOS Apple Silicon via Lima's `vz` backend; on Linux it runs the VM through the host's QEMU with KVM (see [Linux](#linux) below).
- **restic**: Per-workspace backup repositories. Downloaded from GitHub releases.
- **desync**: Content-defined-chunking client that fetches the pre-baked Lima image. Downloaded from GitHub releases. macOS/Linux only.
- **uv-shims**: macOS only, and the one payload that is generated rather than downloaded. It holds a single `install_name_tool` shim, which runs Apple's real tool from the toolchain under `DEVELOPER_DIR` (the standard Xcode location when that is unset) or from `/Library/Developer/CommandLineTools`, and exits nonzero when neither is present. uv unconditionally execs a bare `install_name_tool` after downloading a managed CPython, to rewrite libpython's Mach-O install name ([astral-sh/uv#14893](https://github.com/astral-sh/uv/issues/14893)); on a Mac with no Xcode Command Line Tools, `/usr/bin/install_name_tool` is the xcselect stub, which asks macOS to offer the developer-tools install and so raises a system modal on first launch. uv reports the shim's nonzero exit as a non-fatal warning and libpython keeps its as-shipped install name, which is inert here: Minds runs `bin/python3.12`, which links libpython statically, and none of the bundled packages link it either.

Each is placed in the packaged app's resources directory (`Contents/Resources` on macOS, `resources/` beside the executable on Linux; outside the asar archive). The packaged app prepends the `uv-shims`, `uv`, `git`, `lima`, and `desync` directories to the backend child process's `PATH`, and prepends `uv-shims` to the `uv sync` environment setup's `PATH` as well -- it is the only payload on both, because either spawn can be the one that fetches the managed CPython. `restic` and `desync` are also named by explicit absolute path (`MINDS_RESTIC_BINARY`, `MINDS_DESYNC_BINARY`), so their resolution never depends on `PATH` ordering; `restic` is reached *only* that way, its directory never being on `PATH`.

Dev mode reaches the same pinned binaries: it prepends the `git` and `lima` directories to `PATH` and names `restic`, `desync`, and the latchkey curl by absolute path. `lima` matters because `mngr_lima` resolves `limactl` from `PATH` and enforces only a *minimum* version, so a developer's newer system lima would pass the check and then hang agent creation on the 2.1.x forwarder regression the pin exists to avoid.

`uv` is the deliberate exception. Dev runs the monorepo workspace through `uv run --package minds`, against the same `.venv` and `uv.lock` the developer's shell drives, so it uses *their* uv rather than risking lockfile-format skew against shared state from a second pinned one. It is therefore a bundled binary dev neither downloads nor resolves (`BINARIES[].usedInDev`), and `uv-shims` follows it: dev skips environment setup entirely and shadows nothing for the developer's own uv.

There is deliberately no bundled `qemu-img`. The pre-baked image is published, downloaded, and consumed as a **raw** image end to end, so nothing converts it. See [lima-image.md](./deploy/setup/lima-image.md) for the whole pipeline, and "Why the image is raw" below.

### How the shipped binaries are chosen

`scripts/build.js` (`pnpm build`, the first half of `pnpm dist`) is the only stage whose output reaches the app. One upload builds every platform, so it stages one complete resources tree per shipped target, whatever machine runs it (in CI, the arm64 `minds-runner`):

```
resources/darwin-arm64/payload/    uv, uv-shims, git, lima, desync, restic, curl for macOS arm64,
                                   plus a copy of the shared payload
resources/linux-x64/payload/       uv, git, lima, desync, restic, curl for Linux x86_64,
                                   plus a copy of the shared payload
```

The shared payload (wheels, the runtime pyproject + lockfile with the bundled client config, the latchkey bundle) is built once under `resources/shared/` and copied into every target tree; the staging copy is then removed. `todesktop.js` names exactly one source per target under `targetOverrides` (`mac.arm64` gets `resources/darwin-arm64/payload/`, `linux.x64` gets `resources/linux-x64/payload/`) and no base `extraResources` list: the build service shipped the base list's bytes to Linux under every `platformOverrides` shape tried, which is how three builds produced Linux packages with no tools or with arm64 Mach-O tools (the spec records them). Both sources are directories named `payload` because ToDesktop pairs the per-target lists by `to` plus the source directory's name. The runtime resolves the same paths (`paths.getResourcesDir()`, which is `process.resourcesPath`) on every target. `SHIPPED_TARGETS` in `download-binaries.js` is the list of targets, and `assertStagedExecutablesMatchTarget` refuses a staged directory whose executables are the wrong format for it, by reading the ELF or Mach-O header of every provisioned binary. That assertion is what would have caught the first Linux AppImage, which shipped the build host's arm64 Mach-O tools inside an x86_64 app.

What `build.js` stages is `scripts/download-binaries.js`'s `BINARIES` table, iterated -- not a list written out a second time. Naming the downloaders individually is what shipped `desync`, and later the latchkey `curl`, staged by nothing that reaches the app.

`extraResources` is the only channel that reaches the shipped app, so `appFiles` excludes `resources/` wholesale (`'!resources/**'`) -- anything it matched would be packed into `app.asar` as a second copy nothing reads. The upload carries both targets' tools, so `uploadSizeLimit` in `todesktop.js` is sized for it; `assertUploadFitsToDesktopLimit` prices every platform's list and fails the build before the upload if the estimate exceeds the limit.

Two things would put a duplicate copy back, so neither is wired:

- **`todesktop:beforeInstall`.** ToDesktop runs a hook script against `app-wrapper/app/`, so anything it downloads is folded into `app.asar`. Its agent is x86_64, so the binaries it fetches are Intel ones inside an arm64 app -- unreachable *and* unrunnable. `scripts/build.js` is the only stage whose output ships.
- **`mac.additionalBinariesToSign`.** The builder's signing preflight rejects a listed path that is missing from the app-files upload, so every entry pins its subtree into that upload. It buys nothing: ToDesktop deep-signs every Mach-O under `Contents/Resources` with `mac.entitlements` whether or not it is listed.

### Why the image is raw

Lima consumes the pre-baked image directly as raw, so the app ships no image-conversion tool.

`limactl` embeds `go-qcow2reader` and a pure-Go `nativeimgutil`. Its `proxyimgutil` prefers the `qemu-img` binary but falls back to the Go implementation when it is absent (`exec.ErrNotFound`), and `EnsureDisk` auto-detects the base disk's format (raw, qcow2, or asif). The `vz` driver's `diskImageFormat` defaults to **raw**, with a `convertRawToRaw` fast path. Verified by booting a Lima VM from a raw base disk with `qemu-img` absent from `PATH`: it reached `READY` with a working guest.

Raw is also what `desync` chunks, so publishing raw means the assembled bytes are the bytes Lima boots -- the manifest's SHA-256 covers exactly the image that runs. An earlier design converted the assembled raw to qcow2, which Lima then converted straight back to raw.

Raw costs no extra disk. On the real 20 GiB image the sparse raw occupies **4.9 GiB** on disk versus **5.1 GiB** for the qcow2: qcow2's L1/L2 and refcount tables, plus its 64 KiB cluster granularity, cost more than the filesystem's 4 KiB-granular holes. Only the apparent size differs (`ls` reports 20 GiB, `du` reports what is allocated), so tools that do not understand sparse files will inflate it.

### macOS Intel (x86_64) is not supported

Only the arm64 mac artifacts are built: the Intel (x64) and universal mac targets are disabled in the ToDesktop dashboard (the config schema exposes no arch selection, so the dashboard is where that decision lives), and `.github/workflows/minds-launch-to-msg.yml` fetches and verifies arm64 only. Supporting Intel would need a `darwin-x64` entry in `SHIPPED_TARGETS` plus either a per-arch `extraResources` mapping or `lipo`-merged universal binaries, and a pre-baked x86_64 Lima image, without which an Intel app's prefetch reports `VERSION_UNAVAILABLE` and builds in-VM anyway.

### Linux

Linux x86_64 ships as two artifacts of every build, both packaged by ToDesktop from the same upload: a `.deb` and an AppImage. The `.deb` is the recommended install; the AppImage is for distributions without `dpkg` and for running without installing. ToDesktop's "Add APT sources to the system" is set to **No** in the dashboard, so the `.deb` adds no apt repository -- updates come through the app, exactly as on macOS. `linux.category` is `Development` and `linux.noSandbox` is `probe`, pinned by `appBuilderLibVersion`: the launcher passes `--no-sandbox` only where its probe (`unshare -Ur true`, run from `/bin/sh`) finds no unprivileged user namespaces, which is the case under the AppArmor restriction Ubuntu 24.04 and later ship, so the app starts there rather than aborting. That probe answers for the shell, not for the app. The `.deb`'s post-install script installs an AppArmor profile for `/opt/Mind/minds` whose `userns` rule lets the executable itself create the namespaces Chromium's sandbox needs (the script's other fallback, a setuid `chrome-sandbox`, never applies there because it tests user namespaces as root, which always succeeds). So a `.deb` launched with the flag relaunches itself without it when it finds itself under that profile or beside a root-owned setuid helper (`electron/linux-sandbox.js`; the relaunched process carries `--minds-sandbox-relaunched` so it can never loop, and every packaged Linux launch that saw the flag logs a `[sandbox]` line to `electron.log` saying whether it relaunched, is the relaunched process, or is running without the sandbox and why). The replacement is started by a detached shell that waits for the old process to exit (`electron/linux-relaunch.js`), not by Electron's `app.relaunch`: on Linux that goes through a relauncher helper that Chromium's process launcher starts with the kernel's one-way `no_new_privs` flag (its default for child processes), and a process with that flag can never run a setuid helper, so the `.deb` updater's `pkexec dpkg -i` would fail with "pkexec must be setuid root". Verified on Ubuntu 26.04 on 2026-09-14: launched directly, the packaged binary runs with the namespace sandbox. Debian 12 has no such restriction (and the `.deb`'s post-install script declines to load the profile there, since Debian 12's AppArmor predates the `userns` rule), so there the probe passes, no flag is passed, and both artifacts run sandboxed from the first launch with no relaunch. The AppImage has neither a profile nor a setuid helper on the distributions with that restriction, since nothing can install one for a user-owned mount, so it runs without the Chromium process sandbox there; the app's own isolation of workspaces (containers and VMs) is unaffected either way.

**Installing.** `sudo apt install ./minds-<version>-amd64.deb` installs under `/opt/Mind` with a menu entry and the `minds://` scheme handler registered by the package. The AppImage needs `chmod +x` and, on the distributions that no longer ship it, `libfuse2`; on its first launch (and every launch after, so the entry follows the file) it writes `~/.local/share/applications/minds.desktop` and its icon and registers itself as the `minds://` handler with `xdg-mime`, best-effort. `electron/linux-desktop-entry.js` renders the entry.

**Local backends need host prerequisites.** Nothing is bundled for them. The create form probes the machine when it loads its defaults (`desktop_client/local_prerequisites.py`) and, under whichever local backend is selected, shows what is missing with a copyable install command and a "Check again" button, so every backend stays selectable:

| Backend | Needs | Probe |
|---|---|---|
| Docker | a reachable Docker daemon | `docker version` reports a server |
| gVisor (`runsc` runtime) | Docker plus `runsc` registered as a runtime | `docker info` lists `runsc` |
| Lima | the host architecture's QEMU (`qemu-system-x86_64` or, on arm64, `qemu-system-aarch64`) and a usable `/dev/kvm` | both present and `/dev/kvm` readable and writable |

The create form's local preset prefers Docker on Linux and Lima on macOS, taking the other when the preferred backend's prerequisites are missing (`local_launch_mode` in the form defaults). The install commands are apt one-liners on apt systems and a link to the project's install guide elsewhere. While the selection the form would submit has an unmet prerequisite (the local preset's backend, a compute mode picked in Advanced, or the runsc runtime) the Create button is held, since the create would only fail, slowly, where the notice already says what to do.

**Updates.** electron-updater picks its Linux driver from `resources/package-type`: `deb` selects `DebUpdater`, which installs the downloaded package with `dpkg -i` under `pkexec` and so prompts for the user's password; an absent file selects `AppImageUpdater`, which replaces the running AppImage file in place. On Linux an update is never installed at quit: the update-ready card offers **Install and restart**, the install runs only on that click, and for a `.deb` the card says the password prompt is coming (`electron/install-policy.js`; on macOS the policy stays install-on-quit). From the click until the app quits, the card and the Settings panel show an installing state (the button held, the copy naming the password prompt on a `.deb`); it is set in the renderer before the main process is asked, because the `.deb` install runs the package tool synchronously and blocks the main process for its whole duration, so nothing pushed from there could reach the window in time. An install that does not go through (the prompt cancelled, `dpkg` refusing the package) leaves the app running with the download still staged; the card and the Settings panel say so, and the button is live for another try. A quit cancelled at the running-workspaces prompt after a successful install is reported back to the renderer (the install call settles only then), so the control comes back rather than staying held in an app that is not quitting. An install that did go through but whose quit was cancelled at the running-workspaces prompt has already replaced the package on disk, so the next click on the button only quits, into the new version. After a Linux install the app restarts itself through the same detached shell the sandbox relaunch uses (`relaunchedBy: 'app'` in the policy table; `electron/linux-relaunch.js`) rather than through electron-updater: the `.deb` updater's `app.relaunch` would hand the replacement `no_new_privs`, so it could never run `pkexec` for the update after this one, and the AppImage updater would start the replaced file at once, before this process has quit, where it dies on the single-instance lock and its arrival raises the window over the running-workspaces prompt (`electron/appimage-updater.js` keeps that updater's file replacement and drops its start). An AppImage restarts from the AppImage file with no arguments, since the mounted executable is gone once the old process exits and the file's launcher adds what the executable needs. The running-workspaces prompt and the other quit-time dialogs are owned by the app's most recent window, so a raise of that window cannot hide them. The AppImage updater needs the `APPIMAGE` environment variable the AppImage runtime sets, so an extracted AppImage (`--appimage-extract`) reports updates as unavailable. If two copies of the AppImage exist, only the one being run is replaced.

**Known limitations.** The latchkey gateway keeps running after the app quits, as on macOS. There is no Linux launch-to-message flow in CI yet: `minds-launch-to-msg.yml`'s `linux_artifacts` job extracts both artifacts on an Ubuntu runner and checks that every bundled tool is an x86-64 ELF that runs, that the `.deb` carries `package-type`, and that the bundled wheels resolve into a working `minds` (`scripts/verify-linux-artifacts.sh`); creating a mind on Linux is verified by hand rather than in CI, and [next_deploy.md](deploy/next_deploy.md) lists what the first stable `linux` listing carries with it.

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

`MINDS_ROOT_NAME` selects which data root the backend uses: unset or
`minds` is production, `minds-<env-name>` is another env, with the derived
`MNGR_HOST_DIR` / `MNGR_PREFIX` / `MINDS_CLIENT_CONFIG_PATH` exported
alongside it. Two envs
activated in parallel shells (or by two Electron instances pointed at
two different bundled configs) never share state. Standalone `mngr`
invocations ignore `MINDS_ROOT_NAME`.

### Environment selection

Production is the default. A source checkout with nothing exported
(`apps/minds/scripts/start-desktop.sh`, or a bare `uv run minds run`) reads the
in-repo production `client.toml` and owns `~/.minds/`. Another env is
selected by exporting `MINDS_ROOT_NAME` / `MNGR_HOST_DIR` / `MNGR_PREFIX` /
`MINDS_CLIENT_CONFIG_PATH` for it (Imbue's internal operator tooling does
this); `minds run --config-file <path>` overrides the config path either way.

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

The cooldown only bites during **resolution** -- `pnpm install` without `--frozen-lockfile`, `pnpm add`/`update`, and `uv lock`/`uv add` or a re-resolve. Frozen installs (CI's `pnpm install --frozen-lockfile`, and `uv sync` replaying an up-to-date lockfile) replay the committed lockfile and are unaffected. ToDesktop's build agents are not frozen: they run `pnpm install --no-frozen-lockfile`, which re-resolves on every build, so a version the cooldown refuses fails every desktop build at "Configuring App" even while CI is green. If you add or update a dependency and pnpm/uv refuses a version that is too new, either wait out the window or, for pnpm, add a targeted exception via `minimumReleaseAgeExclude`. The current exceptions are `latchkey`, `@imbue-ai/detent`, and `playwright` / `playwright-core` (latchkey pins playwright to an exact version, so a latchkey bump can otherwise land a playwright still inside the window).

### Running locally

```bash
apps/minds/scripts/start-desktop.sh   # pnpm install + pnpm start, from any directory
```

The script selects the pinned Node, requires the pinned pnpm, installs the
Electron dependencies, and launches the app in dev mode against production.
On Linux, `apps/minds/scripts/install-linux.sh` installs every prerequisite
above (and Docker) and writes a launcher that runs this script; see
[dev-setup.md](./dev-setup.md).

In dev mode, the Electron app skips `uv sync` and uses the monorepo's workspace venv directly (via `uv run --package minds` from the repo root). That installs the `minds` package's own dependency closure, which includes the agent-type plugins the default workspace template configures and the modal provider plugin minds enables; other plugins (e.g. ovh) are available only if the venv already has them (e.g. after `uv sync --all-packages`). Changes to the Python code are picked up immediately on restart.

### Building for distribution

```bash
pnpm build                        # Prepare resources
pnpm exec todesktop build         # Upload to ToDesktop for native builds
```

ToDesktop builds the macOS arm64 native installer (.zip / .dmg) and the Linux x86_64 packages (.deb / AppImage) from one upload, and handles code signing, notarization, and the auto-update infrastructure. Which targets it builds is set in the ToDesktop dashboard (mac Intel and universal are off, the `.deb` is on); how each platform's resources are staged is in "How the shipped binaries are chosen" above, and the Linux specifics are in "Linux". There is no Windows target, and no packaged Linux arm64 build: `download-binaries.js` provisions linux/arm64 binaries for dev-mode runs only (exercised on a Raspberry Pi; see [raspberry-pi.md](./raspberry-pi.md)). The release pipeline (`minds-launch-to-msg.yml`) verifies the macOS app end to end and the Linux artifacts structurally.

The build script (`scripts/build.js`) builds a wheel for every workspace package into the shared payload (`resources/shared/wheels/`), rewrites `[tool.uv.sources]` in the staged `resources/shared/pyproject/pyproject.toml` to point each workspace package at its bundled wheel, then runs `uv lock` in-place to regenerate `uv.lock` beside it against the rewritten pyproject. The shared payload is then copied into every target tree, so the app reads them as `<resources>/wheels/` and `<resources>/pyproject/`. The regenerated lockfile is what ships in the app bundle; the dev-time `electron/pyproject/uv.lock` is not committed.

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
    shell.html              # The loading document: first-launch intro, parked mark + status, quitting and error screens
    intro-timing.js         # The intro's schedule as numbers (electron-free, unit-tested)
    startup-log.js          # Which startup lines the loading document's log may show (electron-free, unit-tested)
    startup-routing.js      # Pure first-window route decision (electron-free, unit-tested)
    assets/
      icon.svg              # App icon (SVG source)
      icon.png              # App icon (PNG for Electron)
      mind-wordmark.svg     # The brand lockup the loading document shows (the SPA inlines its own copy for the start titlebar)
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
