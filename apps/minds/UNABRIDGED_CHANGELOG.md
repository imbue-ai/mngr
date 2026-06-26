# Unabridged Changelog - minds

Full, unedited changelog entries consolidated nightly from individual files in `apps/minds/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-25

Error reporting is now controlled by the user instead of environment variables.

On first launch (and once after upgrading), Minds shows a consent screen -- the "Help improve Minds" screen -- that explains error reporting and lets you opt into "Report unexpected errors" and "Include logs" (both default off; "Include logs" only appears once reporting is on). The same two toggles live permanently on a new Settings page, reached from the "Settings" entry in the sidebar, and take effect immediately, with no restart.

Error reporting is no longer tied to having an Imbue account: choosing "Continue without an account" now also shows the "Help improve Minds" consent screen before you reach the create form, so anyone can opt in (or out).

Sentry now always initializes, but what it sends is gated live by these settings: with reporting off, automatic errors are never sent; with reporting on but logs off, errors are sent without log/traceback attachments. Manual bug reports (see below) are always sent regardless. The `MINDS_SENTRY_ENABLED` and `MINDS_SENTRY_S3_UPLOADS` environment variables no longer gate any of this.

This now covers every layer: the browser web UI and the Electron main process report automatic errors only, so both honor the same "Report unexpected errors" setting as the backend. A page boots its Sentry SDK only while reporting is on (re-evaluated on each navigation), and the Electron shell drops main-process events while it is off (re-checked per event) -- both without a restart. The `MINDS_SENTRY_ENABLED` opt-in switch has been fully retired.

A new "get help" button (question mark) sits in the top bar next to the inbox. It opens a modal with two options: "have an agent help fix the problem" (coming soon, disabled for now) and "report a bug to Imbue". Reporting a bug sends your description to Imbue along with a few basics (version, OS), plus a set of optional, top-level checkboxes: app diagnostics (accounts, known workspaces, system resources), workspace details (when opened from a workspace), and a recorded "remote access" request flag. Those checkboxes remember their last state across reports. Logs are attached when the include-logs setting is on, or via a one-off checkbox when it is off. When a report is sent, the modal shows a copyable Sentry reference ID you can quote when following up. The same report can also be submitted by an in-workspace agent through a new authenticated `/api/v1/agents/<id>/report` endpoint.

In dev (`just minds-start`), the Electron main process no longer starts the native-crash (Crashpad) reporter that the Sentry SDK enables by default. Now that Sentry always initializes, its Crashpad handler would spawn and outlive the app on quit, holding open the stderr pipe that the dev launcher reads; `just minds-start` then hung after every quit instead of exiting. Packaged builds are unaffected and keep native-crash reporting.

Interrupting the app (Ctrl-C / a normal shutdown) no longer files a spurious error report. A KeyboardInterrupt reaching the process is now dropped before it is sent to Sentry, and a clean process exit is ignored too; a genuine fatal exit (non-zero exit code) or any real error during shutdown is still reported.

The get-help modal now dims the workspace behind it (revealing it through a translucent backdrop) instead of covering it with an opaque page surface, matching the inbox modal.

The error screens now carry a report button too. The workspace-recovery page (workspace unresponsive / backend unreachable) has a "Report a problem" link that opens the same get-help modal. The full-app error takeover also has a "Report a bug" button: when the backend is still running it opens the full modal, and when the backend is down it files a report of the on-screen error directly from the app shell and shows the reference ID. That backend-down report attaches basic host info (version, OS, CPU/memory/disk), and -- mirroring the get-help flow -- your recent logs (including the backend's own log, which is still on disk) when you have opted into log inclusion: the "Include logs" setting, or a one-off "Include recent logs" checkbox shown on the error screen when that setting is off. So a crash report is useful even when the backend that would normally gather that detail has stopped, while still respecting your log-inclusion choice.

The workspace-recovery diagnostics now match the forever-claude-template's move from its custom bootstrap service manager to supervisord. The in-container probe that used to read `services.toml` (now deleted) instead asks supervisord directly whether the system interface is running (`supervisorctl -c /code/supervisord.conf status system_interface`) and sources the inner port from the `[program:system_interface]` section of `supervisord.conf`. The port-listening and inner web-server checks are unchanged. A stale `tmux ls` capture of the old per-service windows (which no longer exist) was dropped.

The "Workspace misconfigured" recovery state is gone. It existed only to flag a `services.toml` that failed to declare the system interface -- a shape that can no longer occur. A system interface that is down while the container is up and reachable now classifies as "unresponsive" and offers an in-place restart, which is the correct recovery; the supervisorctl status is shown as diagnostic detail rather than steering a separate screen.

Docs updated to describe the supervisord model (services are `[program:*]` sections supervised by supervisord, started by the bootstrap which execs `supervisord -n`; deferred-install is the one-shot `[program:deferred-install]`).

Show the welcome / sign-in screen whenever the desktop app is functionally empty -- signed out of every account and with no workspaces -- so signed-out users are nudged to sign in again before using the app.

Previously a leftover window from a prior session (e.g. a plain home/`/` window) counted as "restorable" at startup and silently reopened, landing the user on the create page even with no accounts and no workspaces. The cold-start landing decision now treats "no accounts AND no workspaces" as empty and routes to `/welcome` regardless of any stale window-state. A signed-out user who still has workspaces is unaffected (they land on home / their restored windows, not a welcome wall).

The startup landing precedence (welcome > create > restore) was extracted into a pure `electron/startup-routing.js` helper and is covered by `node:test` unit tests (`pnpm --dir apps/minds test:unit`).

"Continue without an account" on the welcome screen now goes straight to the create page instead of first opening a confirmation dialog explaining what an account unlocks.

Reworked the "create a mind" screen into a simpler two-step flow. Instead of a name + color + a stack of provider dropdowns, you now just choose where to run the mind: "Imbue Cloud" (recommended) or "Directly on your computer", as two preset cards. The full provider / repository / branch configuration is still available behind an "Advanced Configuration" link on the same page (with a "Back to simple configuration" link to return); picking a card just fills those advanced fields with that preset's defaults.

The workspace name and color are now chosen automatically -- the name as the next free `mind-N` (the smallest `N` not already used by an existing workspace across any provider, so a gap left by a destroyed `mind-2` is reused before climbing to `mind-4`) and the color as the first unused palette entry -- so neither is asked for on the create screen.

Choosing the Imbue Cloud (remote) preset while signed out no longer bounces you to a separate sign-in page when you click the card. Clicking a card only selects it, and the button stays labelled "Create". Pressing "Create" with Imbue Cloud selected while signed out opens an in-page sign-in / sign-up modal (with an explainer about what running on Imbue Cloud needs) layered over the create screen; once you sign in, the screen reloads in place and you press "Create" again to start creation. If you sign up with a new email and password from that modal, you verify your email as usual and are then returned to the create screen to continue -- instead of being dropped on the accounts page and losing your place. Closing the modal leaves you on the picker to run locally instead. If you are signed in but have the account picker on "No account (private project)" with Imbue Cloud selected, pressing "Create" outlines the picker in red with a message below it explaining you must pick an account (it is no longer shown proactively while you click between the two presets); it clears as soon as you choose one. (Picking the local "Directly on your computer" preset still needs no account at all.)

The "Imbue Cloud" (remote) card is now selected by default on the create screen for everyone, including users without an account -- previously a user without an account landed with the local "Directly on your computer" card preselected.

Removed the three onboarding questions ("Is it OK if I get to know you?", "What should we start with?", "How do you want to deal with permissions?") that used to appear between submitting the create form and the setting-up screen. Submitting the create form now goes straight to the workspace setup/progress screen; this onboarding step is moving into the workspace template itself. The related backend (the answer-applier that ran a local user-context scan, sent the initial problem to the chat agent, and wrote a permissions preference into workspace memory) and the `POST /api/create-agent/{id}/onboarding` endpoint were removed as well.

Refined the look of the two preset cards on the create screen. The selectable edge is now an outline rather than a border: an unselected card shows a thin (1px) dashed neutral outline and the selected card a 2px solid blue outline (with no fill tint), with the change eased so it animates instead of snapping. Because outlines do not take up layout space, the card and its contents no longer shift when selection moves between them. The cards also respond to the pointer -- lifting up 1px (with the existing shadow) on hover, then settling back down and shrinking slightly (a 0.99 scale) while pressed. The "Recommended" badge on the Imbue Cloud card is now bold with normal letter spacing (previously wide-tracked and not bold). The cards' feature-list text now uses the primary (full-contrast) text color rather than the muted secondary one, so it matches the card headings.

Swapped the plain checkmarks in the preset cards' feature lists for the new "badge-check" icons, sized at 16px and nudged down 2px to sit on the text line. The Imbue Cloud (remote) card uses a filled badge in the accent blue, and the local "Directly on your computer" card an unfilled outline badge that simply inherits the color of its adjacent feature text. (The two icons -- `badge-check` and `badge-check-filled` -- were added to the shared 16px icon set, so they are now available throughout the desktop client.)

Adjusted the spacing and layout of the create screen. There is now more breathing room (48px) under the "Where do you want to run your mind?" heading, and 64px above the "Create" button. The account picker and "Advanced Configuration" toggle line, previously above the preset cards, now sits below them (32px under the cards), with the advanced configuration controls opening 32px below that line. The "Advanced Configuration" / "Back to simple configuration" toggle is no longer accent-colored -- it now uses the same muted styling as the welcome screen's "Continue without an account" link (tertiary text, darkening on hover). Shortened the Imbue Cloud card's "Agents run even if your computer is off" feature line to "Runs even if your computer is off". On that account/advanced-config line, the two controls are now pushed to opposite ends instead of being centered with a middot between them: the account picker is left-aligned under the left card and the "Advanced Configuration" toggle right-aligned under the right card. "Advanced Configuration" no longer has a trailing ellipsis, and "Back to simple configuration" now uses an up arrow instead of a left arrow.

In the sign-in modal, the explanatory line ("To run your mind on Imbue Cloud, sign in or create an Imbue account...") now sits directly below the heading rather than above it, and uses the primary (full-contrast) text color instead of the muted secondary one. It replaces the per-tab subheading ("Sign up to enable sharing" / "Sign in to your Imbue account"), which is now hidden in the modal, and tracks the active tab so it reads correctly under both the "Create account" and "Sign in" headings. The standalone auth page is unchanged -- it still shows those subheadings.

The create screen's advanced configuration now has a "Name" field at the top, so you can name the mind exactly instead of always getting the auto-generated `mind-N` name. Leave it empty to keep the automatic name; an invalid name is rejected with an inline error (and your typed value is preserved).

Tidied the advanced configuration layout to match the design: the helper captions for the rows that pair a label with a dropdown (the AWS-credentials note under "Compute provider", and "Where the machine is created..." under "Region") now sit beneath their label on the left rather than under the dropdown on the right. A dashed divider now separates the provider / region dropdowns from the repository / branch fields below.

The create screen's sign-in modal now opens in the app's true overlay layer -- the same full-window layer that hosts the requests inbox -- so it covers everything, including the title bar, and is centered over the whole window rather than only over the create page's content area. (Previously it was an in-page dialog inside the create page, so it could not cover the title bar.) Behavior is otherwise unchanged: pressing "Create" with Imbue Cloud selected while signed out brings it up, a successful sign-in returns you to the create screen (signed in) to press "Create" again, signing up takes you through email verification and back to the create screen, and closing it leaves you on the picker to run locally.

The `MINDS_WORKSPACE_NAME` dev override (honored only under the `MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1` opt-in, e.g. from `just minds-start`) is now used verbatim instead of being auto-suffixed on collision: a pinned name that is already taken now errors at create time -- exactly like a name typed into the form's "Name" field -- rather than being silently renamed to `mindtest-2`. Only the automatic `mind-N` fallback (used when no name is submitted or pinned) still searches for the next free name.

Bump bundled Latchkey version to 2.19.1.

Report frontend (browser) JavaScript errors from the desktop client's web UI to Sentry, mirroring the existing Python backend error reporting. Every page rendered through the JinjaX `Base` layout now boots the vendored `@sentry/browser` SDK (`static/sentry.browser.min.js`, booted by `static/sentry_init.js`) so unhandled errors in the web UI are captured.

Frontend reporting reuses the backend's single opt-in switch (`MINDS_SENTRY_ENABLED`, default off) and environment selection (activated minds env -> production / staging / development), so enabling Sentry lights up the backend and frontend together under the same environment, release, and `git_sha` tag.

All of minds' JavaScript -- the browser web UI and the Electron main process -- reports to one shared set of JavaScript Sentry projects (production / staging / dev), kept separate from the backend's Python projects (a single Sentry project is tied to one platform, so mixing a Python and a JavaScript SDK in one project is discouraged; the browser and Electron SDKs are both JavaScript and share one project set fine). The DSNs currently ship as placeholders -- no Sentry bootstrap/init happens until real DSNs are filled in (`imbue/minds/utils/sentry/frontend.py` for the browser, and a synchronized copy in `electron/sentry.js` for the Electron main process), so a misconfigured DSN never breaks the page or the app.

Report Electron main-process errors to Sentry via `@sentry/electron`, initialized as early as possible in `electron/main.js` so startup failures (window creation, env setup, backend spawn) are captured. It reuses the same `MINDS_SENTRY_ENABLED` opt-in, environment selection, release + `git_sha` tagging, and (per the above) the same JavaScript Sentry projects as the browser web UI.

Fixed: a permission request whose `agent_id` is not a valid `agent-…` id (e.g. an agent that hand-crafts the `POST /permission-requests` body and supplies a placeholder like `ENV_AGENT`) no longer crashes the `latchkey-permission-requests-consumer` thread. Such ids are now rejected up front at the gateway (see the `mngr_latchkey` changelog), so they never reach the consumer. As defense-in-depth, the consumer loop now catches *any* error while processing a single streamed request, logs it with a full traceback, and skips just that request instead of letting the error kill the thread — which previously stopped every permission request, for any agent or service, from reaching the UI until restart (and re-crashed on the same record each restart). The desktop client also continues to hide from the list any request whose agent can't be resolved.

## 2026-06-24

Hardened the Sentry error-reporting path for failures that happen *inside* Sentry event processing (the `before_send` hook, Sentry callbacks, and the HTTP transport).

Consolidated both legs of reporting such a failure into the `log_error_inside_sentry` helper: it now always records the failure in the local app log (so it is never lost) and reports it to Sentry via a minimal event. Previously only the `before_send` path logged locally, so failures in Sentry callbacks and the transport left nothing in the local log.

The local log line is marked so a new loguru filter on the Sentry event handler drops it, preventing it from becoming a second, separate Sentry event.

Made `log_error_inside_sentry` non-reentrant. Reporting goes through `capture_event`, which re-runs the whole `before_send` chain; if the failure being reported originates in `before_send` itself, that previously recursed until the stack was exhausted. The helper now drops nested calls so reporting is attempted at most once.

Fixed the ToDesktop build (and the scheduled `minds launch-to-first-message` health check) failing on every commit since the Tailwind v4 migration. ToDesktop's cloud builder installs the app with `pnpm recursive install --prod`, which omits devDependencies but still runs lifecycle scripts; the `postinstall` hook compiled CSS via the `tailwindcss` CLI (a devDependency), so the install aborted with "command not found" before any platform could be packaged.

Removed the `postinstall` hook entirely rather than guarding it. Compiling CSS is a build-artifact step, not an install step, and the install never needed it: packaged builds already ship the compiled `app.min.css` inside the minds wheel (force-included by `scripts/build.js`), and `pnpm start` compiles + watches the stylesheet via its `prestart` hook. The CSS is now built only at its genuine consumption points.

Released minds v0.3.3. Bumped the app version and pinned the baked `FALLBACK_BRANCH` to the `minds-v0.3.3` forever-claude-template tag. This release also carries the Tailwind v4 CSS migration fix: CSS is now compiled at its real consumption points (packaging, dev, e2e) instead of in a `postinstall` hook, so ToDesktop's `pnpm recursive install --prod` cloud build no longer fails.

Switched all minds Vault reads/writes to the "split" secret layout. Each service entry is now a Vault *directory* whose children are single-field leaf secrets (`secrets/minds/<tier>/<service>/<KEY>` holds `{"value": ...}`), instead of one flat KV entry with many fields. `read_vault_kv` now lists the service directory and reads each leaf's `value` to reconstruct the `{key: value}` dict callers expect, `write_vault_kv` writes each key as its own leaf, and `delete_vault_kv` removes every leaf. Deploy, host-pool, paid-admin, generation-id, and deployment-test code paths are unchanged at the call site but read the new locations only; there is no fallback to the old flat layout. Vault setup, host-pool, and staging-bringup docs were updated to document the split layout and corrected the example `vault kv put`/`get` commands.

## 2026-06-23

Fixed the local-Docker Electron e2e test (`test_create_local_docker_workspace_via_electron`), which started failing once the discovery-health watchdog landed.

The desktop client now spawns the discovery-pipeline supervisor (`mngr latchkey forward`) and the imbue_cloud account-discovery poll (`mngr imbue_cloud auth list`) from `$HOME`, matching its other laptop-side `mngr` invocations (notably the `mngr forward` consumer). Previously they inherited minds' working directory, which in a dev checkout is the monorepo root, so their `mngr` children loaded `<repo>/.mngr/settings.toml`. Under a pytest run that tripped mngr's config guard: the supervisor never started, the discovery producer emitted no snapshots, and the new watchdog escalated the stall to a terminal BLOCKED app takeover that tore down the page mid-creation. This also removes the repeated "Failed to list imbue_cloud accounts" warnings that spammed every dev session.

Minds now watches the health of its own workspace-discovery pipeline and tries to recover it automatically. The pipeline keeps the workspace list, the per-workspace liveness dots, and the recovery flow up to date; previously, if it silently stalled (the background discovery process stopped producing updates) nothing noticed, and if it crashed outright you only got a notification telling you to restart the app by hand.

The new watchdog detects a stall by watching how long it has been since the last discovery update, and self-heals by re-kicking the discovery producer -- first with a cheap nudge that leaves your open workspace untouched, then, if that doesn't help, a heavier restart. While it is healing in the background, nothing interrupts you: an already-open workspace keeps working and there is no banner, only the existing "time since last discovery" indicator in the providers panel.

If the pipeline can't be recovered -- the healing attempts are exhausted, the discovery consumer died, or a fresh launch never managed a first update -- minds now takes over the whole app with a clear "lost track of your workspaces" screen and a "Restart Minds" button, instead of leaving you stuck on an indefinite "Loading workspace" page or relying on a notification you might miss. The old "Forwarding subprocess died -- restart minds" notification is removed in favor of this screen.

Workspace recovery now reads the host's lifecycle state and its provider's reachability from the same passive discovery data the rest of minds already uses (the workspace list, the providers panel), instead of firing its own separate `mngr list` each time the recovery page loads. Having two independent samplers of the same state could let the recovery page disagree with the sidebar; collapsing to one source removes that inconsistency. As a side benefit the recovery page now responds effectively instantly (it reads in-memory state rather than waiting on a network round-trip), and a dropped connection can no longer strand the diagnostic on a slow probe.

Minds now waits until discovery has actually re-observed the workspace *after* a connection drops before taking you to the recovery page -- not merely until some recent snapshot exists. In the brief window right after a drop, the latest snapshot can still predate the outage and report the container as running even though it just stopped; acting on that would show wrong diagnostics (a stopped container marked "RUNNING") and ask you to confirm a restart for a container that is already stopped. So minds keeps you on the "Loading workspace" screen until a snapshot taken after the outage lands (typically within one discovery poll, a few seconds), then takes you straight to the right place: a stopped container is recognized as stopped and its restart is dispatched automatically with nothing to confirm, a genuinely-wedged-but-running workspace still asks before a disruptive host restart, and a provider that is down routes to the "Can't connect to ..." page (which reconnects you automatically once it recovers). The redirect fires the moment that post-outage snapshot arrives, with no extra delay.

When the workspace's backend can't be reached -- the provider's connector is down, the local Docker daemon is stopped or paused, or your login has expired -- minds now shows a single "Can't connect to ..." page in place of the two nearly identical ones it used to pick between based on the kind of error (a split that could send the same "Docker isn't available right now" situation to either page, and left one of them unable to reconnect you on its own). The page surfaces the provider's own error verbatim (e.g. Docker's "Docker Desktop is manually paused. Unpause it through the Whale menu or Dashboard.") rather than a message minds hand-authors per provider, offers a Retry, and always returns you to the workspace automatically once the backend recovers.

Port the Sentry error-reporting setup into the minds backend. `minds run` now calls `setup_sentry()` during startup (after logging is configured) so the Python backend reports errors (and attaches logs) to Sentry.

Enable the Sentry Flask integration so reported errors from web backend endpoints carry request context (transaction name, URL, method, query string, headers).

Add uploading of log files and traceback-with-locals attachments to S3 for Sentry error reports. The log-collection logic now matches the minds log layout: a flat logs directory (`~/.minds/logs`) containing the live Python backend JSONL log, its timestamp-suffixed rotated siblings, and the Electron log, all gzip-compressed on upload. Sentry's `log_folder` is now the minds logs directory (exposed as `WorkspacePaths.log_dir`) rather than the data directory.

Select the Sentry DSN and S3 bucket from the activated minds env (`minds env activate`): production reports to the production Sentry project and bucket, staging to the staging project and bucket, and every other env (dev-*, ci-*, or no activated env) to the dev Sentry project with no S3 uploads (so dev machines never ship potentially-sensitive attachments off-box).

Gate all of Sentry behind the `MINDS_SENTRY_ENABLED` env var (default off): unless it is explicitly set, `minds run` does not initialize Sentry and the backend sends nothing to Sentry at all.

S3 attachment uploads remain separately opt-in via the `MINDS_SENTRY_S3_UPLOADS` env var (default off, even in production and staging), since the uploaded logs and traceback-with-locals attachments can carry potentially-sensitive data. When enabled, the bucket follows the environment; development never uploads regardless of the flag.

Report the desktop app version (from `package.json`) as the Sentry release and the git SHA the build was cut from as the `git_sha` tag. The Electron launcher passes both to the Python backend via `MINDS_RELEASE_ID`/`MINDS_GIT_SHA` (resolving the SHA live from the checkout in dev and from the build-time `build-info.json` in packaged builds); bare source runs fall back to reading `package.json` and report an `unknown` SHA.

Do not attach any user PII to Sentry error reports: the unused user-context wiring (`global_user_context` / `sentry_sdk.set_user`) has been removed, and `send_default_pii=False` is kept.

Flush Sentry (and any pending S3 attachment uploads) during the desktop client's shutdown teardown, so errors captured late in a session -- including any logged while shutting down -- are sent before the process exits. The flush uses a short timeout so an unreachable Sentry/S3 endpoint cannot noticeably delay app exit.

Disable Sentry performance tracing (`traces_sample_rate=0.0`): minds uses Sentry for error reporting, not performance monitoring, so it no longer emits a transaction per HTTP request. Error reports still carry full request context via the Flask integration.

Added `minds pool backfill-host-keys`, an env-aware wrapper around `mngr imbue_cloud admin pool backfill-host-keys`. It resolves the staging / production host_pool DSN from the tier's `<vault_prefix>/neon.DATABASE_URL` Vault entry (exactly like `minds pool list` / `destroy`), so the operator never hand-passes `--database-url`. Run it once per tier after deploying the host-key-pinning connector to keyscan + record SSH host keys for pool rows and bare-metal boxes baked before the host-key columns existed, restoring leasing / prep for those hosts.

Added `apps/minds/docs/vendor-mngr-sync.md` as the single source of truth for how FCT's `vendor/mngr` is synced: the two mechanisms (`git archive` for reproducible, committed release snapshots; `rsync` for working-tree dev iteration and pool bakes), the shared rsync form and where its exclude constants live (`pool_bake.py`), the three paths that populate `vendor/mngr` from the monorepo, and a note that `vendor/mngr` and `vendor/tk` are plain snapshots -- not git subtrees or submodules.

Pointed `release.md` (the vendor-refresh step) and `host-pool-setup.md` (the `--mngr-source` bake rsync) at the new doc instead of partially re-explaining the mechanisms in place.

Updated the host-pool setup docs to reflect that `mngr imbue_cloud admin server prep` now strictly pins the box's recorded sshd host key (no trust-on-first-use): use `server setup` (OS reinstall, which injects the key) or the one-time `admin pool backfill-host-keys` keyscan before prepping a box.

## 2026-06-22

Replaced the desktop client's runtime Tailwind (Play CDN JIT) with a compiled Tailwind v4 build step. The chrome's styles now come from a single minified, tree-shaken stylesheet (`app.min.css`) built ahead of time from `static/app.css` -- no runtime JIT, fully offline, and smaller. This is the foundation for an upcoming light/dark design-token system.

What changed for developers:

- `static/tokens.css` is gone; its hand-written tokens + component CSS now live in `static/app.css` (the Tailwind v4 source entry), which compiles to the gitignored `static/app.min.css`.

- Build the stylesheet with `just minds-css` (replaces `just minds-tailwind`). It also runs automatically on `pnpm install` (postinstall) and is rebuilt before packaging by `scripts/build.js`.

- `just minds-start` now runs the compiler in `--watch` mode alongside Electron, so class changes rebuild live. Because the sheet is compiled, a new/changed Tailwind class only takes effect after a rebuild.

- The compiled sheet is force-included into the wheel via `[tool.hatch.build] artifacts`; `@tailwindcss/cli` and `tailwindcss` are pinned to exact versions.

Began the light/dark design-token system, starting with text colors:

- New themeable text utilities: `text-primary` / `-secondary` / `-tertiary` (text on the current surface) and `text-inverse-*` (text on an inverted surface). Pure black/white at three alpha steps; regular and inverse mirror each other and swap between light and dark.

- Tokens are built in two layers in `app.css`: a per-mode value layer (`:root`/`.light` for light, `.dark` for dark) and an `@theme inline` token layer. Switching the whole app between modes is a single `.dark` class on `<html>` -- no component changes. A `.light` scope can force a light island under a dark ancestor (and vice versa).

- The dev styleguide (`/_dev/styleguide`) gains a light/dark toggle (persisted in `localStorage`, honored app-wide via a pre-paint script in `Base.jinja`) and a "Text color tokens" section showing both modes side by side.

- Migrated the on-light text call sites off the raw zinc ramp to these tokens (`text-zinc-900/800/700` → `text-primary`, `-600/-500` → `text-secondary`, `-400` → `text-tertiary`) across templates, vanilla JS, and the shared button/input class constants. On-dark / inverse text (e.g. log boxes, the primary button label) is intentionally left until the chrome and button stages.

Added themeable border tokens (next design-system category): `border-subtle` / `border-default` / `border-strong` (Figma's 10% / 16% / 25% alpha), pure black in light and pure white in dark.

- Migrated the border call sites: `border-zinc-200` → `border-default` (standard surfaces), form-control borders (`INPUT_BASE`) → `border-strong` to match Figma's form fields, `border-zinc-300` → `border-strong`, `border-zinc-100` → `border-subtle`. On-dark borders (the bg-black menus / log boxes) and status/accent borders are left for their stages.

- Retired the v4 border-compat shim: the global default border color now resolves to the `border-default` token, so every bare `border` is themeable without naming a color. Standard borders are now slightly more defined and inputs noticeably so, matching Figma.

Added themeable surface + fill tokens (next category): surfaces `bg-surface-primary` (solid base; white in light, pure black in dark), `bg-surface-inverse` (its mirror -- the neutral accent, pairs with `text-inverse-*` for primary buttons), and `bg-surface-overlay` (the inverse color at 20%, for backdrops); fills `bg-fill-subtle` / `-hover` / `-active` (translucent tints; Figma's `selected` dropped as redundant).

- Migrated the background call sites: `bg-white` → `bg-surface-primary` (page, cards, inputs), `bg-zinc-100` / `bg-zinc-50` → `bg-fill-subtle`, `hover:bg-zinc-*` → `hover:bg-fill-hover`, modal/drawer scrims (`bg-black/20-30`) → `bg-surface-overlay`. The primary button is now `bg-surface-inverse text-inverse-primary` with an `opacity-90` hover (it flips: black button/white text in light, white/black in dark). On-dark fixed islands (the bg-black floating menus, bg-zinc-900 log boxes, on-dark white tints) are left for the chrome stage.

- Added `color-scheme: light` / `dark` to the theme roots so native controls (form fields, scrollbars, autofill, caret) render in the right scheme.

With surfaces tokenized, dark mode now renders correctly across page/cards/text/borders/buttons. (Light mode is unchanged.)

Added status / feedback tokens (Figma): `important` / `success` / `warning` / `info` (one solid hue each, mode-independent) + `focus-ring`. Notice / badge backgrounds derive from a single hue via an opacity modifier (e.g. `bg-success/12 border-success/30 text-success`), which adapts to the surface in both modes.

- Migrated the status call sites: Notice and StatusBadge variants, inline status text/boxes, the danger button (`bg-important/10 text-important`), and status pills now use the tokens; the solid success button is `bg-success` with fixed white text. Focus rings (inputs, color swatches) now use `focus-ring`. Link / selection blue is intentionally left as-is (not part of the status set).

Reworked the titlebar to self-theme from the workspace color in pure CSS. The bar derives a black/white contrast from `--titlebar-bg` via relative color (`lch(from …)`) on a `.titlebar-surface` scope and re-bases the foreground tokens on it, so the title and buttons read correctly on any workspace color (dark or light) -- with no JavaScript luminance and no server-side foreground calculation. `chrome.js` now just toggles `.titlebar-surface` alongside `--titlebar-bg`; the `TitlebarButton` and page title are plain tokens (`text-secondary` / `text-primary` / `hover:bg-fill-hover`). Neutral chrome (no workspace) follows the app's own tokens.

- Removed the now-dead foreground machinery this replaced: the SSE workspaces payload no longer carries an `accent_fg` triple, the accent-preview IPC bridge (`content-relay-preload.js` / `main.js`) no longer takes an `accentFg` argument, and `pick_workspace_foreground` (plus its sRGB-luminance helpers) is gone from `workspace_color.py`. `workspace_accent.js` keeps only the `normalizeHex` helper.

Made the always-dark surfaces (the floating workspace menu and the log / terminal / credential boxes) `.dark`-scoped islands styled with tokens (`bg-surface-primary`, `border-subtle`, `text-primary` / `text-secondary`, `hover:bg-fill-hover`, `bg-fill-active` for the selected row) instead of raw `bg-black` / `text-white/NN`. They stay dark regardless of the app theme but now derive their colors from the design tokens.

Added the accent / interactive token (last color family): a single mode-independent `accent` (a blue, `#0069d9`, chosen to clear WCAG AA as link text on both the white and pure-black surfaces) behind links, selected states, focus rings, and progress. Solid for selection / progress (`bg-accent`, `border-accent`); lighter rings and tints derive via an opacity modifier (`ring-accent/40`, `bg-accent/15`).

- Unified the two blues that had coexisted: the form focus ring (previously a separate Apple-blue `focus-ring` token) and the raw Tailwind `blue-600` used for links / selection / progress now both resolve to the one `accent` token. The standalone `focus-ring` token is gone.

- Migrated the call sites: `Link` (and `sharing.js` links), the ghost-Button "link" recipe (`Create` / `Latchkey` "Configure" / "Adjust"), the `Opt` selected state + textarea focus, the color-picker selection / focus rings, the create-flow progress bar + pulse dot, the accent spinner, the form-control focus ring (`INPUT_BASE`), the auth "waiting" notice, and the Landing "Backing up" badge. The styleguide gains an "Accent / interactive token" section (light + dark) and drops `focus` from the status grid.

Tokenized the last hardcoded-neutral component recipes in `app.css` so they theme in dark mode: the `.code-pill` background (`fill-subtle`), the `.opt` onboarding cards (`fill-subtle` background, `border-subtle` / `border-strong` borders + radio), the `.spinner` ring + top (`border-subtle` / `text-primary`), and the color hex-input pill background (`surface-primary`). The spinner now stays visible on dark surfaces too (e.g. the Destroying island), where its near-black top was previously invisible. The intentionally-always-dark elements (color-swatch rims, the close-button-hover red) are unchanged.

Tokenized the dev styleguide page's own chrome (`/_dev/styleguide`) so its light/dark toggle now themes the whole page, not just the demo panels. Migrated the section headers, captions, demo-card frames, and footer file-refs from raw zinc/white onto the design tokens; converted the text/border demo "Dark" panels (and the Icon12 chrome-glyph demo) from hardcoded `#18181b` to `.dark` token islands; and refreshed the link / ghost-link demos to the shipped recipes. Removed the now-obsolete "legacy zinc" text-ramp section (the migration it was waiting on is complete).

Tightened the corner-radius scale to four steps -- `rounded-sm` 2px / `rounded-md` 4px / `rounded-lg` 8px / `rounded-xl` 16px (plus `rounded-full` / `rounded-none`), defined in `app.css` `@theme`. The old 6px and 12px values round down: buttons / badges / inputs land at 4-8px and cards / modals / log boxes at 8px (the previous 12px). `rounded-xl` (16px) is reserved for the largest surfaces. The chrome content frame keeps a structural `rounded-[12px]` -- that 12px still matches Electron's `CONTENT_CORNER_RADIUS` and the OS window's outer rounding, so it stays the one documented exception to the scale. Styleguide radius section updated to the four steps.

Constrained the spacing scale to a fixed subset of Tailwind's native steps. `--spacing` stays the stock `0.25rem` (so `p-1` = 4px, `p-4` = 16px -- standard Tailwind, with all its docs / tooling / IntelliSense intact). Padding / margin / gap are limited to ten steps: `0.5 / 1 / 1.5 / 2 / 3 / 4 / 6 / 8 / 12 / 16` (= 2 / 4 / 6 / 8 / 12 / 16 / 24 / 32 / 48 / 64 px).

- The handful of off-scale spacings were snapped to the nearest step, e.g. inputs/buttons tighten slightly (`py-2.5` -> `py-2`, `px-3.5` -> `px-3`). Width / height / inset stay free for layout (component sizes untouched), and large fixed dimensions keep their explicit `[NNpx]` values.

- The styleguide gains a "Spacing scale" section listing the allowed steps and their px values.

Added two guard tests that hold the scales: padding / margin / gap must use the constrained spacing steps, and corner radius must use `rounded-sm/-md/-lg/-xl` / `-full` / `-none` (no `rounded-2xl`/`-3xl`/`-xs`, no arbitrary `rounded-[..]` except the documented content-frame `rounded-[12px]`). Both scan the authored source while skipping SVG path data. The radius guard caught the floating workspace menu's `rounded-[10px]`, now snapped to `rounded-lg` (8px).

Added the type ramp (Figma): six semantic roles defined as `@utility` in `app.css`, each bundling font-size + weight + line-height (and uppercase + tracking for the section eyebrow), so a text element's role is a single class. Color stays orthogonal -- compose with `text-primary` / `-secondary` / `-tertiary`.

- `type-heading-lg` 24/bold, `type-heading` 18/semibold, `type-label` 14/semibold, `type-body` 14/regular, `type-helper` 12/regular, `type-section` 12/semibold/all-caps. Sizes reuse Tailwind's native steps (24/18/14/12 = text-2xl/lg/sm/xs).

- Migrated every content-text site to a role (strict four sizes): 20px headings collapse to `type-heading` (18), the Welcome 30px splash to `type-heading-lg` (24), and 10/11/13px captions to `type-helper` (12) / `type-body` (14). `font-medium` is dropped app-wide (the ramp is 400/600) -- block roles bundle their weight and inline emphasis is now `font-semibold`. Components (FormLabel, SectionHeader, StatusBadge, Button + inputs, ...), pages, and JS-built DOM all use the roles; the ghost-Button "link" recipe uses `!type-helper`.

- The styleguide gains a "Type ramp" section demoing the six roles. A guard test keeps content text on the roles: no raw font-size utilities or `font-medium` in the authored source (SVG path data skipped); inline `font-normal` / `font-semibold` / `font-bold` stay allowed.

- Fixed a `Notice` regression from the migration: the role swap dropped the separating space in the component's runtime class concat, fusing `my-2` with the variant background (e.g. `my-2bg-info/12`) so every notice banner lost its vertical margin and background tint. Restored the space.

- Fixed the same dropped-space regression in the Landing page's JS-built badges: the role swap turned `text-sm font-medium ` (trailing space) into `type-label` (no trailing space), so the four `'... type-label' + tone` concatenations for the mind container-state, provider-status, and backup-status badges fused into an invalid `type-labelbg-...` class -- silently dropping both the type role and the tone color. Restored the separating space.

Dropped the unused `--shadow-seam` token. It was only ever demoed in the styleguide (no real surface applied it -- the titlebar drop shadow it once named is gone), so the definition, both styleguide demos, and its drift-guard entry were removed.

Added an elevation scale: two box-shadow steps defined in `app.css` `@theme` (generating `shadow-raised` / `shadow-overlay`), with a styleguide "Elevation" section and a guard.

- `shadow-raised` is the subtle hover lift on interactive cards (the prior `shadow-sm` value, so cards are unchanged). `shadow-overlay` is the soft floating shadow for surfaces above the page -- menus, modals, tooltips -- taken from Figma's `minds-elevation-1` (two 8%-black drop shadows: `0 1px 1px` + `0 3px 12px`).

- Migrated the call sites: interactive cards / CardPage / Creating card -> `shadow-raised`; the floating workspace menu (previously a heavy `0 12px 32px` at 25%), the modal, and the inbox panel -> `shadow-overlay` (softer and now uniform). A guard test allows only `shadow-raised` / `shadow-overlay` / `shadow-none` -- raw Tailwind shadow steps and arbitrary `shadow-[..]` are disallowed.

Reorganized the dev styleguide page (`/_dev/styleguide`) into two labeled groups with clearer separation -- **Design System** (the foundational tokens plus the shared icon set) and **Patterns & Components** (the composed primitives) -- and added a sticky left-hand table of contents for jumping between sections.

- The light/dark toggle is now a fixed top-right control: it stays visible at any scroll position and floats over the page (no backing bar). It's rebuilt on the `Button` secondary primitive instead of bespoke button classes, so the styleguide's own chrome uses the design system it documents.

- Moved the 24px / 12px icon catalogs up into the Design System group (icons are a shared primitive ramp, like the color and type tokens); moved the workspace-accent picker and the color swatches down into Patterns & Components.

- Each section is a scroll anchor carrying a `scroll-mt` offset, so a TOC jump lands the heading below the viewport top rather than flush against it. `dev_styleguide.js` adds an `IntersectionObserver` scrollspy that marks the active section's link via `aria-current="page"` (styled in `app.css`).

- The color-swatch demo now shows the same three swatches per row (selected / default / disabled) at both the `md` and `sm` sizes, with a little more vertical space between the two rows -- so the size comparison is apples-to-apples.

- Fixed the selected color swatch's selection ring: the gap between the swatch and the accent ring is now a real transparent gap (via `outline` + `outline-offset`) instead of a hardcoded white rim, so it shows the background in every mode rather than flashing a stray white border in dark mode. Applies to both the settings (`md`) and create-form (`sm`) pickers.

Aligned the `Button` primitive with the Figma button component (node 342-4059). The default (md) size now uses the Figma padding -- `px-4 py-2` (16px / 8px) instead of `px-3 py-2` -- and the variant recipes were reworked:

- **Secondary** has no fill at rest: it's a `border-default` outline with `text-primary`, and only tints (`bg-fill-hover` on hover, `bg-fill-active` on press) on interaction.

- **Ghost** is now exactly secondary minus the border (transparent at rest, same hover/press fills).

- **Danger** is a solid semantic fill -- `bg-important` with white text -- replacing the previous subtle red tint; it dims slightly on hover/press.

- **Primary** (solid inverse surface) and **success** (solid green) keep their fills and now dim via opacity on hover/press to match. Every variant carries a 1px border (visible only on secondary, transparent elsewhere) so all variants render at the same height. Disabled opacity moved from 30% to 40% to match Figma.

- All button sizes now use `rounded-md`, which is 6px (see the radius-scale change below) -- so buttons match Figma's 6px corner.

Redefined the `rounded-md` radius step from 4px to 6px (scale: 2 / 6 / 8 / 16). md is the default control radius, so buttons, form inputs, badges, and color swatches all round at 6px now, matching Figma.

Gave buttons a focus ring drawn **outside** the button via `outline` + `outline-offset` (keyboard focus only, `focus-visible`), so it no longer overwrites the variant border; the offset gap is transparent in every mode.

Aligned form inputs (TextInput / Select / Textarea) with Figma's text field (node 345-4059): 12px padding (`p-3`), a tertiary-colored placeholder, a subtle `fill-subtle` tint on hover, and a focus ring drawn outside the field (`outline-offset`) that keeps the `border-strong` border instead of recoloring it (replacing the previous border-recolor + inner ring).

Lifted the accent color in dark mode to a brighter blue (`#0069d9` -> `#4d9bff`). On the pure-black dark surface the original accent read too dark: low-opacity tints (`accent/15`, `/40`) nearly vanished and link text was hard to read. The brighter dark-mode value keeps links, focus rings, selection tints, and progress legible (and clears WCAG AA as link text on black). Light mode is unchanged.

Decluttered the dev styleguide previews: dropped the redundant "Light" / "Dark" labels from the dual-mode token previews (the white/black cards are self-evident), removed the decorative card frame (border / background / padding) from the single-mode previews (Type ramp, Spacing, Corner radius) so the samples sit directly on the page, and dropped the borders from the corner-radius demo shapes (each is now just its filled shape).

Gave the primary button a pressed state: it dims to 70% opacity on `:active` (a step below the 80% hover) -- toned down from an earlier 60% once the press scale (below) was added to carry the feedback.

Gave the styleguide's floating light/dark toggle an opaque surface background (`.styleguide-toggle` in app.css) so it stays legible while floating over page content; the hover/active fills are composited over that surface as a background-image gradient rather than replacing it (a translucent fill background-color would let content show through).

Updated the status / feedback semantic hues (mode-independent): `success` `#5c8a3c` -> `#0c8106`, `warning` `#d49a2c` -> `#b45300`, `info` `#527ea3` -> `#166fc7`, `important` (error / failed) `#f50d00` -> `#d90c00`. All the derived notice / badge tints and status text pick up the new hues automatically.

Further decluttered the styleguide previews: dropped the background from the Elevation section (the two shadow cards now sit on the page, where the drop shadows still read), and removed the card frame (border / background / padding) from the single-mode component examples (buttons, form controls, spinner, notices, links, icons, badges, opt, oauth, section header, dialog close, the workspace-accent picker). The frame is kept only where it carries meaning: the dual-mode token cards, the colored self-theming titlebar surfaces, the dark sidebar / chrome-glyph islands, the accent-spine card, and the page-container / modal backdrop illustrations.

Tuned the radius scale and a couple of tokens:

- `rounded-sm` moved from 2px to 4px (scale is now 4 / 6 / 8 / 16).

- `Notice` banners are now borderless tinted boxes: an 8%-opacity hue fill (`bg-<hue>/8`) with the hue as text, no border (down from a 12% fill + bordered box).

Refined the titlebar buttons (`TitlebarButton`): the foreground is now always `text-primary` (full contrast, re-based per-workspace by `.titlebar-surface`) instead of resting at `text-secondary` and brightening on hover, and the `nav` variant is a square icon button sized by padding (`p-1.5` around the icon -> 28x28) rather than a fixed `w-8 h-7`. Its flex wrappers (the titlebar nav group in the chrome and the styleguide demo) use `items-center` so the button stays its square size instead of stretching to the titlebar height. The `control` variant (min / max / close) keeps its OS-matching `w-9 h-[38px]` geometry but also picks up the always-`text-primary` foreground.

The titlebar workspace title now uses the `type-label` role (14px / semibold) instead of `type-helper` (12px / regular), in both the chrome (`#page-title`) and the styleguide demo -- so the active workspace name reads as a proper title.

Titlebar buttons now show an accent keyboard-focus outline (`focus-visible:outline-2 outline-accent`, no offset) instead of falling back to the browser's default focus ring, which auto-contrasted to a stray white ring on the dark / colored titlebar.

Reworked the `Select` dropdown to match Figma (node 345-4060): the native OS arrow is hidden (`appearance-none`) and replaced with a themeable `chevron-down` `Icon24` overlaid on the right (inherits `text-secondary`, `pointer-events-none` so clicks fall through), inset from the right edge with room reserved via `pr-8`. Added a `chevron-down` glyph to the `Icon24` set. The width prop now sizes the wrapper that anchors the chevron; the `<select>` fills it.

Restyled the status badges (`StatusBadge`):

- **Neutral** keeps its muted fill but now uses secondary text.

- **Done / Failed / Info** (success / error / info) use the full status color as a solid background with white text, instead of a low-opacity tint with colored text.

- **Warning** uses a dedicated yellow caution surface (`--c-warning-surface`, `hsl(49 100% 50% / 0.2)`) with the warning foreground -- a solid yellow with white text would be unreadable, and a tint of the brown-orange warning hue reads muddy. The warning `Notice` uses the same yellow surface.

- The styleguide now shows a full second row of `xs` badges (every variant) instead of a single "Tiny" badge tacked onto the end.

All buttons now scale to 98% on `:active` (a subtle press-in), animated via `transition` (which also smooths the hover/press color + opacity changes). The primary button's active dim was eased from 60% to 70% opacity to match -- the scale now carries most of the press feedback.

Tuned the button press feedback: the scale-down now animates over 100ms on the standard ease-in-out curve (`duration-100 ease-in-out`, i.e. `cubic-bezier(0.4, 0, 0.2, 1)`), and the press (`:active`) no longer changes color/opacity on any variant -- the press is now scale-only, so the scale carries the whole press feedback.

Listed cards no longer touch: the Manage Accounts page's account cards and the workspace-settings Sharing section's list of shareable chats/interfaces now sit in a `flex flex-col gap-2` container (8px gap), so the spacing is owned by the parent rather than added as margin on the `Card`.

The "Back to projects" back-link now renders at the `type-helper` size (12px) instead of inheriting the larger default body size, so it matches the link-style affordance scale -- applied on the Manage Accounts, Workspace Settings, and Destroying pages.

Lifted the titlebar's red notification dot into a reusable `Badge` component (`templates/Badge.jinja`, from Figma node 330-4472). It has two shapes: the bare 8px `important` dot (the default, shown when no count is passed) and a count pill that shows a number in a solid `important` capsule (`min-w-[16px]` keeps a single digit circular; it grows for wider numbers and caps at "99+"). Added a `type-badge` type role (10px / bold / 12 line-height) for the count text -- the one deliberate sub-12px role, reserved for the compact pill. The styleguide's "Notification badge" section shows both shapes (the 4 / 12 / 99+ pills and the dot) alongside the in-context titlebar example.

Switched the titlebar requests button from the corner dot to the inline count (Figma node 330-4463): the pending-request count now sits in a pill beside the messages icon (icon + badge in a `gap-[3px]` row, the gap collapsing to nothing when there are no requests) instead of a dot overlapping the icon's top-right corner. `chrome.js` already received the count over SSE; it now writes it into the pill (mirroring the 99+ cap) and shows/hides the badge as the count changes.

Fixed the requests badge showing a stray "0" pill when there were no pending requests. The badge was hidden with a `hidden` *class*, but the count pill bakes in `inline-flex`, which beats Tailwind's `.hidden` utility in the cascade -- so the class never hid it. It's now hidden with the native `hidden` *attribute* (whose `[hidden] { display: none !important }` base rule wins), set in the template at rest and toggled by `chrome.js` via `badge.hidden`. The `Badge` docstring documents that callers must hide it with the attribute, not the class.

Fixed the same `inline-flex`-beats-`.hidden` bug on the Landing page's per-row health and backup badges, which were "hidden" by a `.hidden` class and so left an empty, space-taking pill in the row (a phantom gap) when there was nothing to show. Both now drive visibility through inline `display` -- `style="display:none"` at rest and `badge.style.display` in the show/hide paths -- matching how the same file already hides the start/stop buttons and the mind-state badge. (The backup-download link next to them is a plain `<a>` with no baked-in display, so its `.hidden` toggle still works and is unchanged.)

Removed the decorative color/state transitions so interface state changes flip instantly, keeping motion only where it's intrinsic to a component. Hover, focus, and selected-state color/border/background changes no longer fade: the secondary/ghost button fills, form-input hover tint and focus ring, titlebar buttons, interactive `Card` hover, the inbox row + close buttons, the dialog close button, the create-flow account `Select`, and the onboarding `Opt` cards/textarea all switch immediately. The button press is now scoped to `transition-transform`, so the 98% press scale still eases over 100ms while its hover/press color + opacity changes flip instantly. Genuine component motion is untouched: the spinner spin, the creating-screen progress bar and pulse dot, the inbox drawer slide-in, and the disclosure chevron rotation.

Stripped the card chrome off the auth/form page wrapper and renamed it `CardPage` -> `PageNarrowContainer`. It no longer paints a surface: the border, rounding, drop shadow, and redundant background are gone, so it's now a plain centered, max-width column (width + padding only) -- the narrow analog of `PageContainer`. The auth flow (login, signup, signin, forgot/check-email, oauth-close, settings, auth-error) and the workspace-creation form now render their content directly on the page background instead of inside a floating card. Padding (`p-8` default / `p-6` form) and the per-page `max_width` are unchanged.

The styleguide's icon catalogs (24px / 12px) are now left-aligned (`items-start`) instead of centered: both the icon row and each icon-over-label cell align to the start, so each icon sits flush-left above its name rather than centered over a variable-width label.

Folded the dev styleguide's workspace-color (accent hex) picker into the "Accent spine" section and dropped the standalone "Workspace accent" section. The accent spine's left stripe is the only surface in the catalog that renders the per-workspace accent, so the picker now lives right beside its live demo: pick a hex and the stripe updates immediately. The picker is now a single compact row (label + color input + hex readout) with no separate preview swatch, and the TOC loses its "Workspace accent" entry.

Bumped the dark-mode background opacity on the `success` / `error` / `info` notice and badge surfaces so their shape stays visible on the pure-black dark surface. The status hues are deep, so the prior 8%-opacity tint (`bg-<hue>/8`) rendered as solid black in dark mode -- the green / red / blue backing was effectively invisible. The tint now goes through a per-mode surface token (`--c-success-surface` / `--c-important-surface` / `--c-info-surface`), staying at 8% in light mode (unchanged) and lifting to 22% in dark mode. This mirrors the existing `--c-warning-surface` pattern; the warning notice already used a bright caution fill and is unchanged. The `Notice` variants and the styleguide status demos now reference these tokens.

Lifted the status / feedback hues themselves in dark mode (they were previously mode-independent). The deep light-mode hues went muddy and low-contrast on the pure-black surface -- both as foreground text and as the derived notice tints -- so `.dark` now overrides each: `success` `#0c8106` -> `#12be09`, `important` (error) `#d90c00` -> `#fb1f13`, `warning` `#b45300` -> `#ff851a`, `info` `#166fc7` -> `#4396ea`. Light mode is unchanged. Because the notice / badge surface tints derive from the active hue via relative color, they pick up the brighter dark values automatically; status text (`text-success` etc.) is now legible on black too.

Unified the dev styleguide's section headings. Every "Patterns & Components" section (Titlebar buttons, Notification badge, Window controls, Cards, ...) previously used a small uppercase eyebrow (`type-section text-secondary`) for its title, while the "Design System" sections used the larger `type-heading text-primary` heading -- so the two groups read at different levels. The 21 pattern-section titles are now the same `type-heading text-primary` `<h2>` as the Design System sections, so both groups share one heading level. The type-ramp's `type-section` role demo and the modal preview's inner heading are unchanged.

Renamed the `Icon24` stroke-icon component to `Icon16` (and its `ICONS_24` path-data catalog to `ICONS_16`) to match what it actually renders: 16px by default (`md` = `w-4`), never 24px. The "24" only ever named lucide's authoring grid -- the `viewBox` stays `0 0 24 24` -- while every call site draws the icon at 16 / 14 / 20px. All call sites move to the new name (the titlebar nav + requests glyphs, the `Select` dropdown chevron, the Landing workspace-row play/stop/restart/open/settings actions, and the settings button), as does the dev styleguide's icon section (now headed "Icons -- 16px (Icon16)", TOC entry "Icons (16px)", anchor `#icons-16`).

Then replaced the icon set itself with a new 16x16 set from Figma (the "Icon" frame, node 857-5091), dropping every old lucide glyph. The component shell is now fill-based -- a `viewBox="0 0 16 16"` SVG defaulting to `fill="currentColor"` -- because each new glyph is a filled outline (Figma's "Vector (Stroke)" flattened to a single path); Figma's hardcoded black is stripped so every icon takes the parent's text color. `play` is the lone stroked glyph and carries its own `fill="none" stroke="currentColor"` to match the set's line weight; `settings` (drawn on a 15-unit grid) and `chevron-down-small` (a small centered glyph) are nudged into the 16-unit frame with a `<g transform>`.

- The set: `menu`, `home`, `user`, `inbox`, `settings`, `chevron-right` / `-left` / `-down` / `-up` / `-down-small`, `plus`, `close`, `restart`, `arrow-up-right`, `check`, `play`, `pause`.

- Call sites remapped to the new names: the titlebar Back / Forward buttons use `chevron-left` / `chevron-right`, the requests button uses `inbox` (was `messages`), and the Landing workspace-row stop button uses the `pause` glyph (was the square `stop`; its tooltip still reads "Stop mind"). `play`, `restart`, `arrow-up-right`, `settings`, `menu`, `home`, and the `Select` chevron keep their names; the unused `external` glyph is gone. The dev styleguide's icon catalog now lists all 17.

- Folded the remaining hand-rolled inline icons onto the set so every in-app glyph comes from one source: the modal dialog close button (`DialogCloseButton`) and the inbox modal's close button both render `Icon16 close` at 16px; the Latchkey predefined-permission checkmark renders `Icon16 check`; and the floating sidebar's "New workspace" / account rows (`SidebarBottom`) render `Icon16 plus` / `user`. The Google / GitHub brand marks (`auth.OauthIcon`) and the 12x12 window-control glyphs (`Icon12`) stay separate.

- The floating sidebar's per-workspace row icons are built in JavaScript (`sidebar_workspace_row.js`), which can't call the `Icon16` JinjaX component, so they were migrated by hand: `buildIconButton`'s inline SVG shell is now fill-based (`fill="currentColor"`, no stroke) and the open-in-new arrow / settings gear path constants were swapped to the new `arrow-up-right` / `settings` glyphs, so the sidebar rows match the rest of the app.

Tightened and refined the shared form-control shell (`INPUT_BASE`, used by TextInput / Select / Textarea):

- Padding drops from 12px to 8px (`p-3` -> `p-2`).

- The single-line TextInput / Select gain tight leading (`leading-tight`) for a more compact field; the multi-line Textarea keeps `type-body`'s roomier 1.5 leading so its wrapped lines stay legible.

- The hover cue changes from a fill tint (`hover:bg-fill-subtle`) to a darker border edge (`hover:border-stronger`) -- a quieter signal than tinting the whole field. This adds a fourth border token, `border-stronger` (40% black in light / white in dark, vs `border-strong`'s 25%), surfaced in the styleguide's border-token section.

- The Select dropdown chevron moves 4px closer to the field edge (12px -> 8px inset).

The styleguide's Textarea sample is now a natural two-line sentence instead of three meta lines of class names.

Restructured the titlebar into three flex sections sized 1 / 2 / 1 (left controls | title | right controls), so the workspace title always sits in the exact horizontal center of the window regardless of how wide each side's controls are. Previously only the center grew (`flex-1`), so the title was centered within the *leftover* space and drifted off-center whenever the two sides differed in width. Each side now owns its platform's OS window controls: the left section reserves the macOS traffic-light strip with a fixed `shrink-0` spacer div *inside* its flex box -- not a left padding, which (with `box-sizing: border-box`) would clamp the equal-width left section's flex base size up to the padding, making it that much wider than the right and pushing the title ~36px off-center; a spacer instead lives inside the section, which `min-w-0` lets shrink to its 1/4 flex share, so both sides keep equal width and the center stays truly centered. The right section holds the requests button plus the app-drawn min/max/close controls (non-mac). `min-w-0` on each section lets it shrink to its flex share so the ratio -- and the centering -- holds at the 800px minimum window width. The dev styleguide's titlebar demo mirrors the same 1/2/1 layout.

Bump bundled Latchkey version to 2.17.2.

Fix `just minds-build` failing during `uv lock` with "no version of overlay==0.1.0".

`imbue-mngr` now depends on the unpublished workspace package `overlay`, but it was missing from the build's hand-maintained bundled-package lists, so no wheel was built for it and uv fell back to (nonexistent) PyPI. Added `overlay` to all four mirrored lists (`scripts/build.js`, `electron/env-setup.js`, `scripts/build_test.py`, and `electron/pyproject/pyproject.toml`) so it is bundled as a wheel and resolved locally.

Bump the minds desktop app to 0.3.2 and point `FALLBACK_BRANCH` at the `minds-v0.3.2` forever-claude-template tag, so a shipped 0.3.2 binary clones the FCT snapshot it was verified against.

Replaced the minds desktop client's FastAPI/asyncio web stack with a synchronous Flask app served by a graceful cheroot WSGI server.

This is an internal framework swap with no user-visible behavior change: every route, path, status code, header, redirect, and Server-Sent-Events stream behaves as before. Notable internals:

- The bare-origin server (`minds run`) now runs on a threaded cheroot WSGI server instead of uvicorn. cheroot speaks HTTP/1.1 with keep-alive (reusing connections) and streams Server-Sent-Events chunk-by-chunk, matching the wire behavior the prior uvicorn server provided -- which the Electron shell depends on (it consumes the one-time login code with a request that 307-redirects and awaits the followed response before loading the UI). Shutdown is unchanged in spirit -- on SIGINT/SIGTERM it flips the shutdown flag and wakes the live SSE streams *before* the server drains, so streams end cleanly with no tracebacks, then closes the HTTP client, stops the discovery/permission consumers, and drains the root concurrency group.

- Server-Sent-Events endpoints (creation logs, the chrome workspace/events stream) are now plain synchronous generators. One unavoidable mechanism change: a browser that closes a stream is noticed on the next write attempt rather than proactively (WSGI exposes no disconnect signal); stream cleanup still runs and there is no functional or UX difference.

- The WebDAV file server under `/api/v1/files` is mounted directly as a WSGI app (the `a2wsgi` ASGI bridge is gone). The `/api/v1` REST API and the `/auth` SuperTokens pages are now Flask blueprints.

- Removed the `fastapi`, `uvicorn`, `a2wsgi`, `python-multipart`, and `websockets` dependencies; added `flask`, `cheroot` (the keep-alive WSGI server), and a direct `werkzeug` dependency (the dispatcher mount and HTTP-exception handling import it directly).

- Raised the Electron shell's initial chrome-state fetch timeout from 4s to 10s. On startup the backend computes `has_accounts` (a cold `mngr imbue_cloud auth list` subprocess that can take ~5s on the first call) before emitting the first workspace-list event; under the old 4s timeout a slow first call returned no state, which the startup path treated as unauthenticated and routed an already-signed-in user to the onboarding page instead of the create page. The longer timeout absorbs the cold-call latency.

Hardened workspace post-create setup against slow `mngr` invocations:

- Bumped the onboarding permissions-preference `mngr exec` timeout from 30s to 60s, so writing the Q3 permissions preference into the workspace doesn't time out (and abort with exit -15) when host-side `mngr` is slow (e.g. under heavy load or cold provider discovery).

- Added debug logging around each `mngr imbue_cloud …` subprocess (subcommand, elapsed time, returncode, and whether it timed out) so a slow or timed-out post-create operation (Cloudflare tunnel create, backup bucket create, etc.) is attributable instead of surfacing only as a bare "exit -15".

## 2026-06-21

- The Electron e2e workspace runner (`create_workspace_via_electron`) now accepts
  `launch_mode`, `region`, and `account_label`, so it can drive workspace creation
  in compute modes other than local Docker (e.g. Lima, AWS). Used to live-test the
  container/VM restart-recovery behavior.

Multiple developer environments can now safely share a single bare-metal slice box.

`minds pool create --backend slice` now stamps the activated environment into each slice's lima names (forwarded as `--slice-env-name`), so a shared box can attribute every slice to an env and reconciliation only ever touches the right env's slices.

`minds env destroy` now tears down the env's unleased pool slices on their bare-metal boxes before deleting the per-env database, so a destroyed env no longer leaks its baked pool VMs on shared boxes. Leased slices continue to be torn down via their agent's release path.

## 2026-06-20

Deprecated baking new OVH classic VPS pool hosts. Imbue Cloud pool hosts are now baked exclusively as bare-metal slices.

`minds pool create` now defaults to `--backend slice`; `--backend ovh_vps` fails fast (before any Vault / credential resolution) with a deprecation error pointing at `--backend slice`. Existing OVH VPS pool hosts keep working and can still be listed (`minds pool list`) and destroyed (`minds pool destroy`, `minds env destroy`).

The host-pool docs (`host-pool-setup.md` and related) were rewritten around the bare-metal slice workflow; OVH is now described only as the current internal supplier of bare-metal boxes and in a "Legacy OVH VPS teardown" section. The per-tier OVH credentials are reframed as bare-metal box supplier credentials (still required, since they order the slice boxes and tear down legacy VPS hosts).

## 2026-06-19

Fix creating a mind from a remote git URL (e.g. a GitHub HTTPS URL) when no branch is specified.

Cloning a remote repo without an explicit branch left the local clone on a detached HEAD (the no-branch path checked out `FETCH_HEAD` detached, and -- unlike the branch-given path -- nothing renamed it to a real local branch afterward). That left `refs/heads/*` empty, so the downstream `mngr create` mirror push, which only pushes `refs/heads/*` + `refs/tags/*`, failed with `No refs in common and none specified; doing nothing` / `the remote end hung up unexpectedly`.

The no-branch clone now uses a plain `git clone`, which resolves the remote's default branch natively and leaves a real named local branch checked out (whatever the remote's default is -- `main`, `master`, etc.). The explicit-branch path is unchanged (it still uses `git fetch` so that a branch, tag, or commit SHA all work).

Workspace recovery now understands remote (Imbue Cloud) minds, not just local docker/lima ones. When the recovery probe finds that the workspace's provider is unreachable -- your network is down, Imbue Cloud is having an outage, or (locally) the docker daemon is stopped -- it shows a dedicated "Can't connect to ..." page with a Retry button and no restart option, because a restart routes through that same unreachable backend and cannot help. The page reconnects automatically (with a backed-off poll) once the provider is reachable again.

When the provider is reachable but rejects the request for another reason (expired login, no account configured), recovery now shows a plain "Can't reach your workspace" message with the reason instead of offering a restart that cannot fix an auth/account problem.

When the recovery diagnostic can't even list your workspace's provider within its (now much shorter) timeout -- the signature of a full network outage -- recovery now treats that as "provider unreachable" and shows the Retry page instead of the destructive "Workspace unresponsive" page. Previously a dropped connection left the diagnostic spinning on "Loading workspace" for up to two minutes and then offered a restart that could not help.

The "Retry" button on the provider-unreachable page now uses the same prominent, full-width primary-button styling as the "Restart workspace" button, instead of falling back to the unstyled native browser button.

Reworked the in-app `mngr` CLI caller (`MngrCaller`) to stop relying on `multiprocessing`'s fork-without-exec (forkserver), which is unreliable on macOS.

Instead of forking children from a preloaded forkserver, `MngrCaller` now keeps a single pre-warmed, single-use `mngr` process running ahead of time: a freshly execed Python interpreter that has already imported `imbue.mngr.main` and is blocked reading one request off an anonymous socket. Each `call` hands the argv to the waiting warm process over the socket, reads back stdout/stderr/exit-code, and the warm process then exits. As soon as a warm process is claimed for use, a replacement is spawned so the next call again finds one ready.

The transport is an anonymous, connected `socketpair` (no rendezvous file on disk): the parent keeps one end and passes the other end's file descriptor to the child at spawn time. The connection is live from the moment the child is forked, so there is no listen/connect handshake and no readiness polling -- if the warm process is still importing `mngr`, the request simply buffers in the socket until it is ready. This makes the "no warm process ready yet" case correct by construction.

Warm processes are cleaned up on every exit path: the idle one is terminated promptly during graceful shutdown (so it doesn't make the root concurrency group wait out its shutdown timeout and log a spurious "strand did not finish in time" warning), and an orphaned warm process whose parent was hard-killed self-exits when it sees its socket close, so none are ever left hanging around.

This avoids paying mngr's multi-second interpreter+import startup cost on the request path while sidestepping fork-without-exec entirely. No user-visible behavior change to `mngr message` delivery; this is an internal robustness and portability improvement.

Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename: the VPS
provider is no longer Docker-only, so the package and its shape-agnostic base
classes dropped "Docker" from their names (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerHostStore` -> `VpsHostStore`, `VpsDockerError` ->
`VpsError`). Import-only change; no behavior difference.

Bump the bundled latchkey CLI to 2.17.1.

## 2026-06-18

Closed a remaining `agent_creator_test` timeout-flake gap. A separate change raised the shared `_wait_until_finished` poll deadline to 30s to match the creation tests' `@pytest.mark.timeout(30)`, but `test_start_creation_api_key_ai_without_key_fails_with_clear_message` (a fourth caller of the helper) carried no per-test timeout and so was still governed by the global 10s pytest-timeout -- which would pre-empt the 30s poll under heavy parallel CI load. It now carries `@pytest.mark.timeout(30)` like its siblings. Test-only change.

Stabilized the minds agent-creator litellm-key tests under offload CI contention: the `_wait_until_finished` poll deadline was raised from 10s to 30s. It only caps a poll that returns the instant creation reaches a terminal state, so passing tests are unaffected; it just stops heavy test setup under CI load from tripping a spurious timeout (matching the `@pytest.mark.timeout(30)` already on those tests).

## 2026-06-17

Reworked how workspace accent colors interact with the minds app shell:

- Non-workspace minds screens (Home, Create, accounts, inbox, auth, ...) and the startup/quitting/error loading screen now paint a pure-white neutral background instead of the previous light-gray / dark chrome. (Light-mode only for now; a pure-black dark-mode variant is a deferred follow-up.)

- The titlebar now shows the neutral white chrome on those general screens and only adopts a workspace's accent color while you're on a workspace-scoped screen -- the workspace itself plus its settings, sharing, destroying, and recovery screens. Previously the titlebar kept the last-opened workspace's accent even after navigating away to a general screen.

- Removed pure black and pure white from the workspace color swatches, so a workspace's accent can no longer be indistinguishable from the neutral chrome. You can still type either value into the workspace-settings hex input if you want it.

- Cancelling out of the Create form now clears the previewed color from the titlebar and returns it to the neutral chrome, instead of leaving the previewed color stranded on the bar.

Fixed a rare bug where approving a latchkey permission request could fail with "Could not apply grant through the latchkey gateway: ... 500 ... ENOENT" when the agent's per-host permissions file had never been created (e.g. agent creation's finalize/link step was skipped or failed).

The desktop client now self-heals: when a new permission request arrives, it checks whether the host's canonical `latchkey_permissions.json` exists and, if not, recreates it from the agent's opaque permissions handle (read from the streamed request's `target` field) so the user's approval takes effect without re-creating the agent. It also idempotently re-registers the requesting agent in the host's allowlist, covering the case where discovery-time auto-registration skipped the agent while the host file was missing. The underlying gateway extension also now creates the host directory on write, so grants no longer 500 on a missing directory.

`minds pool destroy` (and the `just destroy-pool-host` recipe that wraps it) now fully tears down bare-metal `slice` pool hosts, not just OVH VPSes. Previously destroying a slice either failed trying to cancel a non-existent OVH VPS, or -- with `--skip-vps-cancel` -- dropped the DB row while leaving the slice's lima VM running on the box (a stranded slot).

The wrapper now injects both teardown secrets from the activated tier's Vault (OVH AK/AS/CK and `POOL_SSH_PRIVATE_KEY`) so the underlying `admin pool destroy` can tear down whichever backend the row uses -- the connector's region/keypair conventions are unchanged. `just destroy-pool-host <id>` now works for a slice with no extra flags: it destroys the lima VM (freeing the box slot) and then drops the row.

`minds pool create` now supports a `--backend {ovh_vps,slice}` option (default `ovh_vps`, unchanged behavior). With `--backend slice` it bakes a bare-metal slice (a lima VM carved on a pre-registered, prepped bare-metal box) instead of ordering an OVH VPS.

Both backends resolve the activated tier's secrets from Vault so the operator never exports them by hand: the slice path reads the tier's `pool-ssh` private key and injects it as `POOL_SSH_PRIVATE_KEY` for the carve (mirroring how the OVH path injects the OVH AK/AS/CK and the management public key). The slice path also accepts `--dry-run` (report the chosen server + per-slice sizing without baking) and `--max-concurrency` (cap how many slices bake at once; forwarded to the admin CLI, which defaults to 4), and rejects the OVH-only flags (`--management-public-key-file`, `--no-recycle`) with a clear error.

Added a `minds paid {add,remove,list}` command group (and a `just add-paid-email <email>` one-liner) for managing the connector's paid-user allowlist from an activated env. Like `minds pool`, it resolves the connector URL (from the env's `client.toml`) and the paid-list admin key (from the tier's `<vault_prefix>/supertokens` Vault entry) automatically, so the operator never hand-passes `--connector-url`/`--api-key`.

- Mark the two forkserver-based `MngrCaller` end-to-end tests as `@pytest.mark.flaky` so offload retries them: forkserver cold-start can exceed the 10s pytest timeout under CI load.

`minds pool create --backend slice` now requires `--server-id` (the bare-metal
box to bake the slices onto, from `mngr imbue_cloud admin server list`), forwarded
to the underlying `mngr imbue_cloud admin pool create`. Slice baking now targets
an explicitly-chosen, ready server rather than auto-selecting one.

Gave the three desktop-client litellm-key tests (`test_start_creation_imbue_cloud_ai_with_local_compute_mints_litellm_key`, `..._api_key_ai_does_not_mint_litellm_key`, `..._subscription_ai_does_not_mint_litellm_key`) a `@pytest.mark.timeout(30)` budget (replacing `@pytest.mark.flaky`): they are deterministic sync tests but their setup (fresh ConcurrencyGroups + a recording http-server fixture) can exceed the default 10s pytest-timeout when offload sandboxes are contended. No behavior change.

## 2026-06-16

In the predefined permission request dialog, the catch-all permission is now labelled `all` instead of `any` so it reads more clearly (the underlying value stored and granted is still `any`). While `all` is checked, the specific-permission checkboxes are disabled (they keep their own checked state); unchecking `all` re-enables them.

Sped up in-app `mngr` invocations (e.g. the `mngr message` sent when a permission request is approved or denied) by adding `MngrCaller`, which runs the `mngr` CLI in a child forked from a pre-warmed `multiprocessing` forkserver instead of spawning a fresh Python interpreter each time.

The forkserver imports `imbue.mngr.main` once at app startup (on a background thread, off the request path), so subsequent calls skip the multi-second interpreter-and-plugin import cost. Running in a forked child also keeps `mngr`'s global-state changes (loguru, `sys.argv`, stdout/stderr) out of the long-lived backend process.

`MngrMessageSender` now always routes through this caller (defaulting to the shared, pre-warmed instance), so approving or denying a permission request no longer spawns a fresh `mngr` process. Other direct `mngr` CLI call sites can migrate onto `MngrCaller` incrementally.

Approving or denying a permission request (both file-sharing and predefined-credential dialogs) no longer blocks on the agent nudge: `MngrMessageSender.send` now dispatches the `mngr message` onto a background thread tracked by the app's concurrency group and returns immediately, so the dialog responds without waiting for the (network-bound) delivery to complete.

Correct `apps/minds/docs/release.md`: tag the **verified** mngr SHA, not `main` HEAD.

The merge/tag steps assumed the merged `main` HEAD equals the SHA you built and verified in step 4. In practice `main` can advance past that SHA between verification and merge (unrelated PRs landing), so tagging `main` HEAD ships an unverified, vendor-mismatched tree. `release.md` now:

- **Merge the mngr release PR with a merge commit, not a squash**, so the verified SHA stays reachable on `main` as the merge parent.
- **Tag `minds-v<version>` on that verified SHA** (`GREEN_MNGR_SHA` from step 4) and on the FCT commit whose `vendor/mngr` is its archive — never `main` HEAD.
- The step-6 vendor-match check now verifies the **commit that actually gets tagged** (FCT `origin/main` post-merge, extracted via `git archive origin/main:vendor/mngr`), not the local working copy — so a stale checkout can't pass the check while a different tree gets tagged. A `main`-HEAD mismatch is documented as **expected drift**, not an error.
- The close-loop CI now reuses the already-verified build (the tag is the step-4 SHA), instead of repackaging.
- Step 3 (vendor refresh) now points at the `just sync-vendor-mngr` recipe instead of inline `git archive`, so the doc and the recipe stay in sync, and documents the per-user `FCT_DIR` (set once in a gitignored, minds-scoped `apps/minds/.env`, alongside `GH_TOKEN`) so a release agent knows where to point the recipe, with explicit guidance to ask the user if `apps/minds/.env` is unset.
- The runbook is now copy-paste-correct end to end: a new **Session setup** section defines `GH_TOKEN`, `MNGR`, `FCT`, and `FCT_DIR` once up front (steps 4/6/7 no longer reference undefined vars), **no personal path is hardcoded anywhere** (steps 6/7 use `$MNGR`/`$FCT`), and the FCT-PR-review note's verification command is fixed (`git archive <sha> | tar -x … && diff -r …` — the previous `git archive | diff -r` could not run).

Caught while cutting `minds-v0.3.1`: `main` HEAD had drifted +58 unrelated files past the verified SHA, so the tag was placed on the verified merge parent.

## 2026-06-15

Remove potentially confusing parts of the messages sent to agents after approving or denying permission requests

`minds pool create` gained a `--skip-deferred-install-wait` passthrough (forwarded to the admin bake) for faster dev pool bakes.

imbue_cloud workspace creation now sends the form's repository to the lease, so the fast path can only adopt a pre-baked host that genuinely matches the requested repo (previously the repo was dropped and only an operator-chosen branch label was matched, so a request for one repo could silently adopt a host running another).

- The desktop client passes the create form's repository through as `-b repo_url=<repository>` (a remote URL in production, a local clone path in dev); the imbue_cloud provider canonicalizes it (resolving a local path to its `origin` remote). The client does no git logic itself.

- `minds pool create` (the OVH pool bake wrapper) now takes the bake source as exactly one of `--from-tag <tag>` (production) or `--workspace-dir <dir>` (dev) and derives the stamped identity from it; `--attributes` is optional and must not carry `repo_url` / `repo_branch_or_tag`.

## test: mark a flaky desktop-client timeout test

- `test_start_creation_subscription_ai_does_not_mint_litellm_key` is now `@pytest.mark.flaky`, matching its already-marked API_KEY twin: both occasionally exceed the 10s pytest-timeout when offload sandboxes are contended (unrelated to product behavior). No source change.

minds: bump test_start_creation_imbue_cloud_ai_with_local_compute_mints_litellm_key timeout to 30s wallclock (pytest-timeout 30s, _wait_until_finished deadline 20s -- the 10s headroom lets the helper's "creation X did not finish within 20s" AssertionError surface instead of racing pytest-timeout's generic "Timeout (>30s)" message). The test occasionally exceeds 10s under heavy CI load -- same root cause as its already-@pytest.mark.flaky sibling.

Fix the `macos_launch` cold-launch smoke test to match the redesigned welcome splash.

- The `macos-launch.spec.js` Playwright smoke test (the `macos_launch` job in `minds-launch-to-msg.yml`, run on a vanilla `macos-latest` runner with no auth state) was asserting the old login screen: a `Create` link or a `Log in` **button**. The welcome screen was redesigned to a "Welcome to Minds" splash where `Sign Up` / `Log In` are now **links** (`ButtonLink` -> `<a>`) and there is a `Continue without an account` button. The stale `getByRole('button', { name: /^log in$/i })` selector matched nothing, so the test timed out at 120s and failed the whole launch-to-first-message run even though the app launched fine and the real `launch_to_msg` job (which auths via one-time code and drives the logged-in UI) passed.

- The smoke test now asserts against the **content window** (`pickContentWindow`) rather than `firstWindow()`. `firstWindow()` races between the `/_chrome` title-bar view (which carries no auth/landing UI) and the content view, so the old assertion intermittently inspected the wrong window and timed out even when the app had launched correctly.

- On the content window it keys off stable structural hooks instead of visible copy / role: the welcome splash is detected via the skip-account button id (`#skip-account-btn`) or the login link href (`a[href="/auth/login"]`), and the logged-in home via the `Create` link. Any one proves the cold-launch path completed, and a future wording or link-vs-button redesign of the splash no longer breaks the test.

- Updated `test/e2e/README.md` to describe the new welcome-splash landing state and to point at the current workflow (`minds-launch-to-msg.yml` `macos_launch` job, twice-daily schedule + dispatch) instead of the retired `minds-macos-launch.yml`.

- Fixed the `launch_to_msg` job's `mngr list` cross-check, which was aborting with `Provider 'aws' references unknown backend 'aws'` (empty output -> W1 looked absent). Root cause: the cross-check ran the bundled mngr inheriting the e2e's cwd (the mngr monorepo checkout), and mngr's project-config discovery walks up from cwd to the git-worktree root and reads `<root>/.mngr/settings.toml` -- which declares an `[providers.aws]` block for the repo's own image builds. A bundled mngr without the `aws` plugin rejects that project-layer block, aborting the parse before any agent is listed. This is a pure artifact of the cwd: minds.app spawns `mngr forward` / `mngr list` with `cwd=$HOME` (see `forward_cli.py` and `laptop_agent_types_seed.py`), where there is no `.mngr/` project layer. The cross-check now runs the subprocess with `cwd=Path.home()`, matching production exactly, so it reads only the minds host profile and never the repo's config. Parse stays strict, so a genuinely bad host-profile config still fails loudly. Verified against the bundled mngr: from the repo root the list aborts (exit 1); from `$HOME` it lists cleanly (exit 0).

- Completed the bundling of `imbue-mngr-aws` so the packaged-app build resolves again. The AWS-provider work added `imbue-mngr-aws` as a minds dependency but did not add it to the four bundled-workspace-package lists (`scripts/build.js`, `electron/env-setup.js`, `scripts/build_test.py`, and `electron/pyproject/pyproject.toml`'s `[project] dependencies` + `[tool.uv.sources]`). Without a local-wheel source override, the build's `uv lock` resolved `imbue-mngr-aws` from PyPI, where the packaged app's 14-day dependency cooldown (`exclude-newer = "14 days"`) rejected the freshly-published `v0.1.1` -- failing `pnpm dist` on every build (including `main`'s scheduled runs). Added `imbue-mngr-aws` to all four lists (the `test_workspace_package_lists_are_consistent` drift guard enforces their agreement), so its wheel is bundled and `uv lock` resolves it locally, bypassing the cooldown. Verified: drift guard passes and `uv lock` against the updated pyproject resolves cleanly.

- Hardened the `launch_to_msg` W2-destroy wait against slow lima teardown. The e2e polled `/api/destroying/<id>/status` and raised immediately on `status == "failed"`. But that status is derived fresh per poll as pid-dead + `is_host_still_active` (see `destroying.read_destroying`): the detached `mngr destroy` subprocess exits before the lima VM finishes dropping out of discovery, so on a slow runner the status reads `failed` transiently (subprocess gone, host not yet) before flipping to `done` once the host is actually torn down. A failure probe confirmed W2's VM was already absent from `limactl list` while the destroy was reported `failed`. The wait now polls for actual completion (`done`/404), treating `failed` as "not done yet", and only fails if the host is still active at the 240s deadline -- which still surfaces a genuine silent-orphan teardown. (The destroy endpoint reporting a transient `failed` during normal slow teardown is arguably worth smoothing server-side too, in the provider/destroy layer the AWS-era "destroy whole host" change introduced.)

Cut minds `0.3.1` and standardize the release tag convention on the `minds-v<version>` prefix across both repos.

- The minds release now tags **both** `imbue-ai/mngr` and `imbue-ai/forever-claude-template` with the same `minds-v<version>` tag (e.g. `minds-v0.3.1`). mngr already used the `minds-` prefix (`minds-v0.3.0`, `minds-v0.2.4`); FCT was the odd one out on a bare `v<version>` (`v0.3.0`). The `minds-` prefix namespaces minds-app releases so they don't collide with either repo's own `v<version>` / package versioning.
- Bumped `apps/minds/package.json` `version` to `0.3.1` and `templates.py` `FALLBACK_BRANCH` to `"minds-v0.3.1"` (the FCT tag the shipped binary clones at runtime).
- Rewrote `apps/minds/docs/release.md` for the **`main`-based, two-PR flow**: a release is two PRs (mngr + FCT) that both target `main`, proven green as a pair, reviewed, merged, tagged `minds-v<version>` on each repo's `main`, then re-verified against the tags (which concludes the release). It documents the **vendor-match invariant** (FCT `vendor/mngr` must be the `git archive` of the exact mngr SHA it is tagged with), and records that the Apple-Silicon lima-VZ `cryptography` SIGILL is handled by the FCT template's `OPENSSL_armcap=0`, not an mngr pin.
- Hardened the `launch_to_msg` e2e diagnostics. (1) Snapshot names are now slugified before use as filenames (`_safe_snap_name`): a failed-redirect snapshot embedded the agent's `/recovery?return_to=...` URL, and the `?` made `actions/upload-artifact` reject the whole artifact -- which lost *every* diagnostic for the failing run. (2) The whole-desktop `screencapture` "could not create image from display" failure (expected on the headless CI runner, which has no Aqua session) is downgraded from a per-snapshot warning to a single debug line; the Playwright per-page screenshots remain the real diagnostics there.

## 2026-06-14

Added **AWS** as a compute-provider option in the workspace create form, alongside Docker, Lima, Vultr, and Imbue Cloud. Selecting AWS launches the workspace in a runsc-hardened Docker container on an Amazon EC2 instance (the same outer-host/container model as the Vultr and OVH providers, so the secure latchkey gateway runs on the EC2 host outside the agent's container).

- The create form requires picking an AWS region (from the regions with pinned default AMIs) and shows an inline note that AWS credentials are read from the environment (`AWS_*` / `AWS_PROFILE` / `~/.aws`).

- The existing "Cloud" compute option was renamed to "Vultr" to name its provider plainly.

- The workspace listing now shows a compute-provider label on every row (AWS, Vultr, Docker, Lima, Imbue Cloud).

- AWS hosts are long-lived: they never idle-shut-down and have no max-lifetime timer.

- minds writes one per-region AWS provider block into its mngr settings at startup (only when AWS credentials are present) and ensures the region's security group exists before each create.

- minds now suppresses the default region-less `aws` provider in its mngr settings (the same way it already suppresses the default `imbue_cloud` provider), so `mngr list` no longer logs a spurious "credentials not configured" discovery warning every cycle. The usable AWS providers remain the per-region `aws-<region>` blocks.

Destroying a workspace now reliably tears down its entire host, fixing a bug where a destroy could report success in the UI while the underlying cloud instance kept running (and billing).

- Destroy always tears down the whole host (the workspace agent plus the per-host `system-services` agent), so the cloud instance is actually terminated. The previous single-agent fallback -- which could remove only the workspace agent and leave the host alive -- has been removed.

- The destroy no longer shells out to a slow `mngr list` to find the workspace's host: the host id is immutable and already known from in-memory discovery. If the host genuinely can't be determined, the destroy is refused with a clear error instead of doing a partial teardown.

- A workspace now stays visible (as "destroying", then "failed" if teardown didn't finish) until its host is confirmed gone, and is only removed from your account at that point. A failed or partial destroy no longer silently vanishes from the UI while the host keeps running.

- The AWS region picker now offers only the US datacenters (`us-east-1`, `us-east-2`, `us-west-1`, `us-west-2`) by default. Each configured region adds a provider that `mngr list` queries every discovery cycle, and the non-US regions roughly doubled listing latency for little benefit.

## 2026-06-13

Fixed: a stopped or unresponsive workspace could get stranded on the "Loading workspace" loader and never advance to the recovery page. The desktop shell decides to show the recovery page from a one-shot "system interface status" event; if the chrome window reloaded after a workspace went stuck, that status was lost and never replayed, so the auto-redirect never fired even though the backend had correctly detected the stuck workspace.

Two changes close the gap:

- The Electron shell now replays the latest non-healthy workspace status when a chrome/sidebar view (re)loads, so a reloaded window re-learns which workspaces are stuck and redirects to the recovery page.

- The backend's chrome event stream now periodically re-asserts non-healthy workspace statuses (in addition to the existing connect-time snapshot and per-transition pushes), so a desynced window self-heals within about 15 seconds even if it missed the original event.

Also fixed: clicking into a mind whose container the landing page already shows as "Stopped" no longer waits through the multi-second stuck-detection window before a restart begins. The landing page now routes a known-stopped mind straight to the recovery page, which confirms the host is offline and cold-boots it immediately, instead of loading the workspace and waiting for repeated probe failures to first declare it stuck.

Fix agent creation from a local git worktree (the dev `minds-start` flow, and any local-worktree source): `clone_git_repo` now checks out the fetched ref so the clone has a materialised working tree, matching what `git clone` produces. A recent rewrite that swapped `git clone` for `git init` + `git fetch` (to accept commit SHAs) dropped the checkout, so the worktree-overlay rsync's files landed untracked and the follow-up checkout aborted with "untracked working tree files would be overwritten by checkout", failing the create. This affected docker and lima local-worktree creates.

## 2026-06-12

The file-sharing permission dialog now accepts `~` / `~/...` notation for the current user's home directory when editing the path to share. The path is expanded to an absolute home-directory path (mirroring the gateway), the client-side within-roots check and Approve gating expand it too, and `~user` notation for another user's home is rejected with a clear error.

Internal: routed the agents-root directory construction through the shared `get_agents_root_dir` helper (now in `imbue.mngr.hosts.common`). No behavior change.

# minds

`_build_mngr_create_command` now passes `--vultr-region=<region>` instead of `--vps-region=<region>` to the inner `mngr create --provider vultr` subprocess. The shared `--vps-*` build-args prefix was retired in this branch and the Vultr provider now rejects it with a migration error, so the CLOUD launch mode would otherwise fail on every host creation. The accompanying unit-test assertions are updated to the new prefix.

Pool-host docs now point at the canonical `minds pool create` flow (via the
new `just bake-pool-host` / `just list-pool-hosts` / `just destroy-pool-host`
recipes) instead of the low-level `mngr imbue_cloud admin pool create` recipe
with hand-exported OVH creds and a hand-passed `--management-public-key-file`.
The env-aware wrapper derives the management SSH key + OVH credentials from the
activated tier's Vault entries automatically; for staging/production it also
resolves the host_pool DSN from Vault.

`minds pool {create,list,destroy}` now resolve the staging/production host_pool
DSN from `secrets/minds/<tier>/neon.DATABASE_URL` themselves (alongside the OVH
creds and management key they already read from Vault), so the commands work on
those tiers without a hand-passed `--database-url` even when invoked directly.
An explicit `--database-url` still wins, and dev/ci continue to auto-resolve the
DSN from their per-env `secrets.toml`.

Did a broader accuracy pass over the minds docs, fixing things that had drifted
since the Vultr->OVH pool migration:

- Replaced stale Vultr references in the pool / env-teardown flows with OVH
  (environments.md, vault-setup.md, host-pool-setup.md). Vultr mentions that
  describe the CLOUD launch mode are left as-is -- that mode still uses
  `--template vultr`.

- Corrected the Modal app names in vault-setup.md (`llm-<tier>` / `rsc-<tier>`,
  not `litellm-proxy-<tier>` / `remote-service-connector-<tier>`).

- Fixed the `minds run` config-resolution description in design.md and
  overview.md: there is no implicit `client.toml` fallback -- `minds run`
  refuses to start when neither `--config-file` nor `MINDS_CLIENT_CONFIG_PATH`
  is set.

- Fixed the Electron backend invocation in desktop-app.md (`run`, not
  `forward`, and it passes `--config-file`).

- Dropped the stale `--id <id>` flag from the `mngr create` examples
  (design.md, user_story.md) -- minds reads the agent id back from the
  `created` JSONL event.

- Corrected `minds` -> `minds run` (user_story.md), `mngr events` -> `mngr
  event` (latchkey-permissions.md), the spurious `kv/` Vault path prefix
  (host-pool-setup.md), and the broken `apps/minds/scripts/install.sh` install
  snippet in the README (replaced with the real from-source dev flow).

Pending permission requests whose originating agent's host can no longer be resolved (for example, after the agent's workspace has been shut down) are now hidden from the desktop client inbox instead of rendering with raw agent ids. The request is left untouched on the gateway, so it reappears in the inbox if the workspace comes back. The inbox badge count and the rendered cards are driven off the same filter, so they stay in agreement.

## 2026-06-11

Replace the SHA-derived per-workspace accent with a user-pickable palette + custom hex.

- Workspaces now ship with one of 12 named palette colors (in picker order: `confusion`, `courage`, `envy`, `peace`, `belonging`, `energy`, `strength`, `comfort`, `inspiration`, `clarity`, then the two neutrals `indifference` and `white`) or an arbitrary `#rrggbb` hex chosen by the user. The previous SHA-from-agent-id OKLCH hue is gone.

- A palette-only picker is added to the **Create** form at the top, above the launch / AI provider configuration. The selected color is written as an mngr `color=<hex>` label on the new primary agent at create time -- no follow-up write.

- A fuller picker is added to **Workspace settings** above the Account section: the same 12 swatches plus an always-visible hex input that accepts lenient forms (`#fff`, `fff`, `#ffffff`, `ffffff`, any case) and normalizes to `#rrggbb` lowercase on save. Save is implicit -- a swatch pick saves immediately; a typed hex saves on blur. Inline errors cover invalid hex, the workspace being unreachable, and the underlying `mngr label` shell-out failing. Picker controls disable when the workspace's provider is in error state.

- Titlebar text / nav icons / account button now use a **WCAG relative luminance** contrast picker server-side, so legibility holds across the full hex range -- previously a fixed black-on-light assumption. The foreground RGB triple is emitted as `accent_fg` on each SSE workspaces payload entry; the client just drops it into a CSS variable.

- Color edits propagate live: the settings POST endpoint shells out to `mngr label <agent> -l color=<hex>` (CLI merge semantics, so concurrent writes against other label keys don't clobber each other), updates the resolver's snapshot optimistically, and fires the SSE wake-up so the chrome / sidebar / homepage tile repaint within one tick.

- Workspaces created before the picker shipped that still have no `color` label render as `confusion` (`#0b292b`, the default) until the user picks something. The first save persists the choice as an on-disk mngr label.

- Sidebar item spines on the dark sidebar (`bg-zinc-900`) currently paint the stored hex unchanged; dark palette entries (`indifference`, `confusion`, `courage`, `envy`) read as low-contrast spines on that surface. A separate PR will rework the sidebar treatment to address this.

The sidebar is now a floating menu: dark panel with rounded corners,
shadow, and a colored dot per workspace, matching the Figma "Space switcher
menu" design. In Electron the page loads into the shared modal
WebContentsView (transparent background), so the panel reads as a floating
overlay above the workspace content. Each row's accent is shown by the dot
alone -- the old left-edge vertical accent stripe (carried over from the
docked sidebar) is removed as redundant.

Every workspace row reveals its per-workspace settings gear on hover (and
in Electron, an "Open in new window" button alongside it); the current
workspace's row shows those icons at all times. Two new rows at the bottom
of the menu: "New workspace"
(navigates to /create) and "Manage account(s)" / "Log in" (replaces the
account button that used to sit in the titlebar). The titlebar no longer
shows the account button.

The sidebar behaves like a modal: clicking anywhere outside the menu (or
pressing Escape) closes it. The menu's height comes from its own flex
layout -- no JS measurement or per-bundle bounds math.

Each window now hosts three WebContentsView surfaces instead of four:
chrome (titlebar), content (workspace), and a single shared overlay used
by both the sidebar and the inbox. The sidebar URL (/_chrome/sidebar) is
loaded into the same modalView that hosts /inbox, so dismissal,
titlebar-drag suppression, transparent background, and Escape handling
all come from the existing modal infrastructure.

The menu's position is now driven entirely by the call site, not by an
inferred ``is_mac`` flag. The chrome page reads the trigger button's
``getBoundingClientRect`` and passes the rect + a caller-chosen offset
through; the menu anchors at ``trigger.bottom-left + offset`` regardless
of where the trigger lives. In Electron that goes over IPC into
``/_chrome/sidebar``'s query string (``trigger_x`` / ``trigger_y`` /
``trigger_w`` / ``trigger_h`` / ``offset_x`` / ``offset_y``); in browser
mode chrome.js sets the inline panel's ``style.left`` / ``style.top``
directly. The panel uses ``py-1.5`` (vertical padding only) so the
row's ``px-2`` lines up exactly with the trigger button's icon offset
inside its ``w-8`` shell -- icon columns line up automatically. Moving
or restyling the trigger button in the future requires no template
changes.

An incoming permission request no longer yanks the open menu away. Now
that the sidebar and the inbox share one overlay view, auto-opening the
inbox is gated on no modal already being visible (previously it only
checked whether the *inbox* was open, so it would load the inbox over an
open sidebar). When a menu is up, the request surfaces via the live
titlebar badge instead, and auto-opens once the menu is dismissed and
the next request arrives.

On macOS the titlebar's left padding grew from 72px to 76px so the first
titlebar button's hover highlight clears the window's traffic lights with
a little more breathing room. The workspace menu follows automatically
(it anchors to that button's measured position), so no menu-side change
was needed.

The menu's internal spacing was tightened to a uniform grid: 4px padding
on all four sides of the panel, 2px between every entry, and 2px above
and below the divider line (the line is now a bare full-width rule that
takes its spacing from the panel's row gap rather than its own padding).

The menu is anchored 2px left of and 2px below the trigger button's
bottom-left corner (anchor offset (-2, 2)). Its background is a flat pure
black for now (was the dark-teal #0b292b) while the color treatment is
being iterated on.

The workspace row is now a single shared builder
(window.mindsSidebarRow.buildRow) rather than markup duplicated across
the Electron menu (sidebar.js) and the browser menu (chrome.js). The row
carries no outer positioning -- spacing is the parent container's flex
gap -- so it composes cleanly wherever it's dropped in. The styleguide's
"Sidebar items" sample renders through that same builder, so the catalog
can't drift from the live menu.

The workspace menu is now 280px wide (was 244px).

Each workspace row's action icons -- the settings gear and (in Electron)
the "open in new window" arrow -- are now always visible rather than
revealed on hover. The open-in-new icon is the lucide arrow-up-right
diagonal arrow (matching the Figma "Space switcher menu"), replacing the
older external-link box glyph.

The landing page's workspace rows gain the same "open in new window"
arrow, placed just left of the settings gear. In the desktop app it
opens (or focuses) a dedicated window for that workspace; in a plain
browser it opens the workspace in a new tab.

The titlebar button that opens the workspace menu now uses a hamburger
"menu" glyph (three horizontal lines) instead of the old panel-left
sidebar glyph (Figma node 559-5101). The ICONS_24 catalog entry was
renamed sidebar -> menu accordingly.

The settings gear inside the floating workspace menu rows is rendered
smaller (Figma node 560-5111) so it reads as a lighter secondary action
next to the workspace name, rather than competing with it.

Workspace rows now use the same hover highlight (``bg-white/10``) as the
"New workspace" and account rows at the bottom of the menu, so the whole
menu responds to hover consistently. The per-row action icons (open in
new window, settings) keep their glyph sizes but sit in larger 24x24
buttons, giving a bigger, easier click target.

The shared Card component's row layouts (``row`` / ``row-spread``) now use
a tighter ``gap-1.5`` (6px) between children instead of ``gap-3`` (12px),
so the landing-page workspace rows, account rows, and settings rows pack
their badges and action icons more closely.

The desktop client's latchkey services catalog (`ServicesCatalog` / `ServicePermissionInfo`) moved to `imbue.mngr_latchkey.services_catalog` and now reads the bundled `services.json` directly instead of fetching it from the running gateway's `GET /permissions/available` endpoint.

This removes the catalog's dependency on gateway liveness for what is static package data, and lets the same catalog serve both the desktop permission dialog and the server-side credential-sync path. The gateway client's now-unused `get_available_services` method (and its `AvailableServiceEntry` wire model) were removed; the client is otherwise unchanged and still drives the live `permission-requests` and `permissions` extensions.

Replaced direct ValueError/RuntimeError raises in deploy-lifecycle config validation, the forward-CLI envelope stream consumer, and Telegram credential extraction with dedicated custom exception types.

The Electron workspace-creation e2e driver (`create_workspace_via_electron`) now
fails fast when creation fails. It previously only waited for the workspace-ready
redirect, so any `mngr create` failure (e.g. an unregistered docker runtime) made
the driver block for the full 10-minute navigation budget before timing out with
an opaque Playwright error. It now races the workspace-ready URL against the
create flow's failure view (`#failure-view`), raising `WorkspaceCreationFailedError`
with the surfaced `#error-message` text the moment creation fails -- turning a
silent 10-minute hang into an immediate, diagnosable failure. This affects only the
e2e test/snapshot path, not the shipped app (which already surfaces the failure
view to users).

## 2026-06-11

agent_creator: clone_git_repo + checkout_branch now accept commit SHAs in addition to branch and tag names. Implementation switches from `git clone --single-branch --branch <ref>` to `git init && git fetch origin <ref> && git checkout -B <name> FETCH_HEAD`, which is uniform across all three input shapes. Non-shallow, mirror-pushable, no behavior change for existing branch/tag inputs.

## 2026-06-10

# Local-mind shutdown on quit + landing-page Start/Stop controls

- On quit, if any local minds (workspaces on `docker` / `lima` hosts) are still running, the app now prompts to shut them down or leave them running, noting that running minds keep using your computer's resources while shutting them down stops their agents and makes their services inaccessible (your data is preserved). Choosing "Shut down all" waits, with a progress window, for the containers to stop before quitting, and offers Retry if a stop fails. Once every workspace is down it also stops this env's mngr docker state container (the provider bookkeeping container that a host stop leaves running) so nothing minds-related keeps running; it is stopped (not removed), so its data is preserved and it restarts on next use. Programmatic shutdowns (SIGTERM, e.g. `just minds-stop`) skip the prompt.
- The landing page now shows each local mind's container status (Running / Stopped / Unknown) and a per-state control: a Stop button (with a confirmation dialog) when running, and a Start button when stopped. The "Stopped" state suppresses the "server not responding" health badge. Remote minds keep the existing Restart button.
- Container status is read straight from the global discovery snapshot's host state (the same `host.state` discovery already tracks for every mind) rather than from a dedicated liveness poll, so there is no second `mngr list` loop. A user-issued Start/Stop sets a short-lived optimistic override so the badge and quit prompt flip immediately, and the next discovery snapshot confirms it; an externally-driven stop/start is reflected on the next snapshot. Container liveness rides each workspace entry in the existing landing SSE payload rather than a separate event channel.
- Which providers expose host stop/start is gated by a single predicate (`provider_backend_supports_shutdown`, currently the local `docker` / `lima` backends), so the rest of the machinery is provider-agnostic and ready to widen when remote providers gain host shutdown.
- Added `POST /api/agents/{id}/stop-host`, `POST /api/agents/{id}/start-host`, `GET /api/minds/running`, and `POST /api/minds/stop-hosts` endpoints. The single-mind stop/start endpoints run synchronously and return the real outcome; the quit-time bulk stop issues one `mngr stop <ids…> --stop-host` (mngr stops the hosts concurrently) and reports which minds are still running.

- Stopping a mind from the landing page now closes any other window that was open to that mind, instead of leaving it stranded. Previously the stranded window saw the mind's now-unreachable system interface, redirected to the recovery page, and auto-restarted the host -- silently undoing the stop. A window that is itself mid-restart is left alone (so the user's own restart isn't interrupted), and if the open window is the only one left it falls back to the home page rather than closing (which would quit the app).

# Quitting page on app quit

- When a quit is committed, every open window now flips to a full-window "quitting" screen -- the same animated wordmark as the startup loading screen, with a status line -- and stays on it until the app closes. This replaces the previously frozen-looking UI during backend teardown.
- The native prompt asking whether to shut down still-running local minds still runs first and is unchanged; only after you commit (Leave running / Shut down) do the windows flip. Cancelling that prompt leaves the app fully intact with no visual change.
- When you choose "Shut down", the stop progress ("Stopping N minds…") now shows in-page on the quitting screen. The small frameless "Stopping minds…" window has been removed. If some minds can't be stopped, the native Retry / Quit anyway / Cancel quit dialog still appears; "Cancel quit" reverses the flip and returns the app to its normal running state.
- All open windows show the quitting page. Headless quits (`just minds-stop` / SIGTERM) tear down without any interactive UI, as before.

Raised the stale coverage floor from 68% to 70% to match the coverage CI already measures (~72%).

Hardened edge-case handling in `imbue/minds/config`:

- `parse_agents_from_mngr_output` now raises `MalformedMngrOutputError` (instead of silently returning an empty list) when mngr's stdout is empty/blank, and raises it (instead of a bare `KeyError`) when the parsed JSON object lacks an `agents` key. Both cases indicate broken upstream output rather than "no agents".
- The config loaders (`load_client_config` / `load_deploy_config`) now catch the precise `tomllib.TOMLDecodeError` and `pydantic.ValidationError` rather than the broad `ValueError`, so an unrelated `ValueError` bug is no longer mislabeled as a config parse/validation failure.

- Cut the **0.3.0** release of the minds desktop binary. Bumps
  `apps/minds/package.json` `version` to `0.3.0` and repoints
  `FALLBACK_BRANCH` in `apps/minds/imbue/minds/desktop_client/templates.py`
  from `v0.2.35` to the new FCT tag `v0.3.0` (at FCT commit `82a70518`).
  Every provider mode that clones FCT (lima / docker / vps_docker / vultr /
  ovh / imbue_cloud) lands on the same reviewed snapshot.

- The FCT v0.3.0 snapshot is the first release on the simpler-lima
  architecture (FCT PR #150 dropped docker-in-VM, runs agents directly in
  a lima VM as root) with the M5 lima-VZ SVE2 workaround baked in
  (FCT PR #151: `OPENSSL_armcap=0`). Verified end-to-end by launch-to-msg
  CI run 27288878538 with `skip_slack_flow=false` on
  `(mngr wz/minds_onboard, FCT main 82a705185)`.

- minds.app CI: slack permission flow via Playwright clicks now green end-to-end (run 26694320389, 34s drive-slack step total). Sequence: agent receives slack-read prompt -> calls latchkey gateway -> gateway 403s the slack.com call (no perm) -> agent POSTs /permission-requests -> Playwright detects agent's "requested permission" signal in chat -> waits 2s -> clicks button[title="Requests"] in the chrome shell window -> clicks the slack entry in the auto-opened requests panel (text=/slack/i) -> a per-request detail window opens at /requests/<id> -> Playwright clicks button:has-text("Approve") in that window -> gateway POSTs /permissions/rules (rule_key=slack-api) and DELETEs the request -> Playwright types a follow-up "permission approved, please retry" kick (claude won't retry on its own; it's parked on "waiting for approval") -> agent re-calls slack.com/api/conversations.history through the gateway -> mock returns canned MESSAGE_BODY -> agent emits `TOK <nonce>:CI MOCK: greetings from the localhost slack mock.` in chat -> Playwright asserts the canned-body substring lands in the assistant reply.

Iteration burn-down: 10 CI runs in the morning surfaced the layers in order: (1) verify job missed `setup-node@v4` (resolve-build-URL script needs node); (2) `HOST_NAME` env didn't propagate across the pipe to the python3 matcher (`KeyError`); (3) matcher picked the `system-services` agent (`RUNNING_UNKNOWN_AGENT_TYPE`) over the chat agent; (4) initial slack-mock arch assumed the gateway runs in the lima VM, but lsof confirmed it runs on the macOS host with a reverse-SSH tunnel from the VM; (5) macOS keychain trust install needs interactive auth even with sudo + authorizationdb pre-grant; (6) so brew curl + CURL_CA_BUNDLE replaces the SecureTransport curl that ignores --cacert; (7) `latchkey auth set` failed because the shim's keychain lookup hits nothing in a non-TTY shell — read encryption_key from ~/.minds/latchkey/encryption_key explicitly; (8) Playwright's `_electron.launch()` ate `ELECTRON_RUN_AS_NODE=1` leaking through `process.env` and silent-exited within 200ms — strip it explicitly; (9) `pgrep -f '/Applications/Minds.app/...'` was case-sensitive vs the lowercase install path so the entire kill loop was a no-op (every minds process stayed alive) — switch to `pgrep -fi` + `lsregister -kill`; (10) `first-message-verify.sh` grep matched THREE login URLs in the events log (mngr forward emits two on :8421, backend emits one on the random port) and `tail -1` raced — anchor to "Minds login URL" prefix; (11) `mngr event ... --include 'event.type == "assistant_message"'` returned nothing because the in-VM agent events don't reach the desktop client's events log — switch reply detection to `limactl shell <vm> -- tmux capture-pane -pS -500` and grep for the model's `●` bullet; (12) the post-launch Welcome window vs chrome shell race meant the chrome shell with button[title="Requests"] wasn't always present — authenticate via `/authenticate?one_time_code=...` explicitly after Playwright launch; (13) the requests panel doesn't re-render when a request lands AFTER it's been opened, so opening at t=4s left it permanently at "Requests (0)" — wait for the agent's "requested ... slack permission" text in chat before opening (lets the gateway persist first); (14) Approve lives in the per-request detail window at /requests/<id>, NOT the requests-panel window — search the detail URL first; (15) Claude won't retry the gated tool call after approval on its own — send a kick prompt asking it to retry.

The vanilla launch CI (`minds-playwright-vanilla.yml` on `macos-latest`) also stays green. Total goal coverage: launch-to-first-message verified on both vanilla and self-hosted; slack permission flow verified on self-hosted with localhost mock + brew curl + cacert + Playwright clicks. The latchkey gateway runs on the macOS HOST (started by minds.app), not inside the lima VM -- the agent reaches it via a reverse-SSH tunnel back to 127.0.0.1:1989 and the host's gateway makes the outbound slack.com call. So all interception lives on the host: `slack-mock-setup.sh` generates a self-signed cert for slack.com / files.slack.com, installs it in `/Library/Keychains/System.keychain` (so libcurl-darwinssl trusts it), patches `/etc/hosts` to point slack.com to 127.0.0.1, pre-seeds `latchkey auth set slack` via the bundled `/Applications/Minds.app/Contents/Resources/latchkey/bin/latchkey` shim with `LATCHKEY_DIRECTORY=$HOME/.minds/latchkey`, starts `slack-mock-server.js` on 127.0.0.1:8443 (plain HTTP), then runs a sudo socat TLS terminator on 127.0.0.1:443 that forwards to 8443. End-to-end-verifies reach by curl-ing `https://slack.com/api/auth.test` from the host and checking for the canned team name. `first-message-verify.sh` learns `SKIP_DESTROY=1` and writes `/tmp/first-message-agent-info.json` (host_name, creation_id, base_url) so the slack flow can reuse the same agent. `drive-slack-ci.js` kills minds.app, launches its own Electron instance via Playwright, clicks the workspace tile (named after host_name), sends a read-only slack prompt, watches for any "Approve/Allow/Grant" UI and clicks it, then asserts the mock's canned `MESSAGE_BODY = "CI MOCK: greetings from the localhost slack mock."` substring lands in the assistant's reply (not just a nonce echo, which the model could fabricate without the tool ever firing). `slack-mock-teardown.sh` reverses /etc/hosts, removes the trusted cert, clears the latchkey slack auth, and kills the mock+socat in an `always()` step; the agent is then destroyed via the workspace delete endpoint, and `mac-runner-reset.sh` runs as belt+braces. Verify job timeout 25 -> 40 min. `socat` installed via `brew install socat` if missing. Same commit series also fixes three pre-existing bugs in the verify job that had kept every recent run red: (1) verify job missed `setup-node@v4` so `resolve build URL` died with `node: command not found` (the self-hosted runner's nvm-shimmed node isn't on the bash -e PATH); (2) HOST_NAME env var was set only on the left of the pipe in `first-message-verify.sh`'s matcher, so the python3 subshell on the right of the pipe raised `KeyError: 'HOST_NAME'` and the polling loop never broke; (3) the matcher picked the first agent whose `host.name == host` (the FCT-baked `system-services` agent, RUNNING_UNKNOWN_AGENT_TYPE under pilot) rather than the chat agent (whose `name == host`) -- now prefers the chat agent and falls back to host-match only if no name-match agent exists. `.github/workflows/minds-playwright-vanilla.yml` downloads the latest released ToDesktop arm64 zip (or a workflow-dispatch-provided URL), installs to /Applications, runs `launch-smoke.spec.js` headless. No lima, no agent creation, no creds -- covers the cold-launch + UI-renders path on a truly vanilla image (replaces Tart-as-manual-loop with a free hosted-runner equivalent). Triggers: push + PR to wz/minds_onboard + workflow_dispatch. Artifact uploads playwright report on failure for postmortem.
- Adds `apps/minds/test/e2e/CI-DESIGN.md` capturing the three slack mock-integration paths (/etc/hosts + TLS, patch slack.js, register mock service) with the open questions on port 443 binding and the recommended /etc/hosts + socat approach. Slack-flow CI workflow is the next follow-up (will extend `minds-launch-to-msg.yml`'s verify job on the self-hosted MacBook to drive drive-slack.js against a localhost mock server).
- minds.app: add Playwright UI E2E driver scripts under `apps/minds/test/e2e/`. Two iteration aids beyond the spec-runner: `drive.js <step>` runs one numbered step (launch / fill form / submit / observe iframe / send message) so I can debug each phase in isolation with a screenshot per step; `drive-full.js` runs the full home -> Create -> LIMA workspace -> first-message flow with progress polled every 15 s and screenshots per state-change. Both target the user's live `~/.minds/` because the bundled root_name file overrides the `MINDS_ROOT_NAME` env var in the signed CEO build. Drove the flow successfully on commit cee6300e2: launch=4 s, form fill+submit=2 s, URL redirect to /creating/<id>=1 s; lima boot in progress at commit time. (Screenshots dir is .gitignored.)
- minds.app: scaffold Playwright + Electron UI E2E tests under `apps/minds/test/e2e/`. Two specs landed: `launch-smoke.spec.js` (chrome window + Python backend + create-form mount, no lima -- safe for `macos-latest` GitHub-hosted runners) and `chat-roundtrip.spec.js` (creates a LIMA workspace via UI clicks, types a prompt, asserts the assistant reply contains the expected token; requires nested virt, so self-hosted minds-runner MacBook only). Each run isolates state under `MINDS_ROOT_NAME=minds-pw-<runId>` so the user's live `~/.minds/` is never touched. Targets the installed `/Applications/Minds.app` by default; override via `MINDS_APP_PATH` for pre-release artifacts. @playwright/test and playwright pinned to identical 1.60.0 to dodge the dual-version dispatch error. CI workflows that consume these specs come in a follow-up commit -- this drop is the scaffold.
- minds.app: seed `[agent_types.main] parent_type = "claude"` into the laptop-side user-scope settings.toml on every minds startup. The FCT workspace's `[agent_types.main]` block lives at `/code/.mngr/settings.toml` inside the lima VM and on the laptop only in ephemeral `mngr create` temp clones. `mngr forward` and `mngr list` run from cwd=$HOME and can't see either, so they were falling back to BaseAgent for agents whose data.json records `type = "main"`, surfacing as `RUNNING_UNKNOWN_AGENT_TYPE` in `mngr list` output, a `Agent system-services has type 'main' which is no longer registered` warning on every event in `minds.log`, and a broken `mngr message` path that routes through `BaseAgent.send_message` (literal text + Enter) instead of the InteractiveTuiAgent paste-and-submit pipeline Claude's TUI needs. The seed is idempotent (a literal substring check for `[agent_types.main]` skips re-append) and targets only `MNGR_HOST_DIR` under `~/.minds/`, leaving the system-wide `~/.mngr/` install untouched. Empirically verified on the live host: pre-seed, `mngr list` showed `system-services` in `RUNNING_UNKNOWN_AGENT_TYPE`; post-seed, it shows `STOPPED` / `REPLACED` / `WAITING` with the proper agent class resolved.
- first-message-verify.sh: harden the JSON parser. The previous `json.load(sys.stdin)` silently exited on any non-JSON prefix (e.g. mngr's RUNNING_UNKNOWN_AGENT_TYPE warnings going to stdout in some configurations, or partial buffering during writes). Now read all stdin, skip to the first `{` or `[`, and emit a diagnostic line to `/tmp/first-message-mngr-list.txt` if parsing still fails -- so future runs surface the actual cause instead of failing with a generic 'no mngr agent on host'.
- minds.app: bump `_MNGR_FORWARD_LISTEN_TIMEOUT_SECONDS` from 5.0s to 120.0s in `apps/minds/imbue/minds/cli/run.py:87`. The 5s deadline was tight enough to deterministically fail every first-time-user launch on a clean Mac: on a cold install with no `~/.minds/.venv` present, uv has to download the python toolchain and install the venv before `mngr forward` can bind its FastAPI lifespan port, which takes ~30-60s on a fresh machine. Existing installs were unaffected because uv reuses the cached venv. Empirically proven by spinning a vanilla Tart VM of macOS 26.4: cold-start launch of build 260530zg31wiwle failed at the 5s deadline with `mngr forward did not report a listening port within 5s; the plugin likely failed to start`, then succeeded on the same VM after a kill-and-relaunch (warm venv). Bumping to 120s covers cold install with headroom while still surfacing a real wedge before the user gives up.
- minds.app: bump to 0.2.32. Cuts a new ToDesktop build for the install-and-restart prompt fix (`updateReadyAction.showInstallAndRestartPrompt: 'always'`) so the auto-updater feed picks it up as newer than the silently-ships-with-broken-prompt 0.2.31 build (260530zg31wiwle). The only substantive bundle change vs 260530zg31wiwle is the 14-line main.js diff -- everything else in 0.2.31 (SSH transport fix, bundled restic, Mac-runner build fix, pin audit, Lima 2.0.3 pin) is carried forward unchanged. ToDesktop's smoke-test framework explicitly flagged the prior build was unreleasable as an auto-update target because its version (0.2.31) matched the previous released build's version, so no AB upgrade test could run. The bump unblocks the AB smoke test path.
- minds.app: enable the install-and-restart prompt for auto-updates. `main.js` previously called `todesktop.init()` with no options, which falls through to @todesktop/runtime's default `updateReadyAction.showInstallAndRestartPrompt: "never"` -- the runtime downloads the update and silently stages it in ~/Library/Caches/com.todesktop.<appId>.ShipIt/, but never surfaces a "Install now / Install on next launch" dialog to the user. So users saw the initial "Update found, downloading in the background" toast, the download completed (~8 s for a 326 MB artifact), and then nothing -- with no in-app indication that the staged update was ready. Pass `updateReadyAction.showInstallAndRestartPrompt: "always"` to `todesktop.init()` so the runtime shows the native two-button dialog as soon as the staged bundle is ready.
- minds.app: fix indefinite hang at "Transferring git repository..." during agent creation on macOS hosts where `SSH_AUTH_SOCK` routes to 1Password's biometric SSH agent. The shared `build_ssh_transport_command` in `libs/mngr/imbue/mngr/hosts/common.py` (used by git push + rsync) now pins authentication to the explicit `-i` key via `-o IdentitiesOnly=yes -o IdentityAgent=none`. Without these flags, OpenSSH consults `SSH_AUTH_SOCK` first; in BatchMode (no TTY) the 1Password biometric prompt can never fire and ssh blocks forever on the agent reply with nothing surfaced upstream, so the `mngr create` flow stalled silently after the lima VM reached READY. Symptom was that minds.log emitted `Transferring git repository...` once and then went silent for the entire `mngr create` timeout. The hot patch is portable across providers since every git-over-ssh / rsync-over-ssh call goes through the same builder.
- minds.app: bundle restic 0.18.1 per target platform (`resources/restic/restic`) alongside uv / git / lima. `desktop_client/restic_cli.py` now reads `MINDS_RESTIC_BINARY` (set by `electron/backend.js` via `paths.getResticPath()`) before falling back to a PATH lookup, so a fresh install can provision per-workspace restic backups without the user installing restic system-wide. SHA256-verified downloads added to `scripts/download-binaries.js`; signed for Mac via the new `additionalBinariesToSign` entry in `todesktop.js`.
- minds.app build: fix cross-build bug shipping Linux ELF binaries into the Mac bundle. CI's `minds-launch-to-msg.yml` build job had `runs-on: ubuntu-latest`, so `scripts/build.js` + `scripts/download-binaries.js` (which read `process.platform`/`process.arch`) bundled Linux x86_64 `uv`, `git`, and `lima` into `resources/`; ToDesktop then packaged those as-is. The shipped Mac arm64 .app silently failed at first launch when `env-setup.js` tried to exec the bundled `uv` and hit `exec format error` — and we couldn't see it because `runEnvSetup`'s catch block only popped a UI dialog, not stdout. Two fixes: (1) move the build job to `runs-on: [self-hosted, macOS, minds-runner]` so the bundled binaries are native Mach-O arm64 (xcrun for git, astral-sh tarball for uv, lima-vm tarball for limactl); (2) `console.error('[startup] env-setup failed:', err.message)` in main.js so future env-setup failures land in Electron stdout (captured by the verify job's `/tmp/minds-electron.log` thanks to a5458b44c).
- minds.app: bump to 0.2.31. Cuts a new ToDesktop release with the merged 1772 work (todesktop.js dynamic config + beforeInstall hook + pnpmVersion pin), the bundled-git fix from PR #1771 (already in main), the git-SHA bake into the About panel (so the shipped binary is traceable back to a commit via `Version 0.2.31 (<tdBuildId> · <shortSha>)`), and the latchkey host-resolution fallback (still standalone in PR #1793 -- will return via main once merged). Engines pinned to node 24.15.0 + pnpm 10.33.4 per the new `engines` block in `package.json`; ToDesktop reads both from `package.json` via `todesktop.js`.
- minds.app: bake the build's git SHA into `electron/build-info.json` at `pnpm build` time and surface a short SHA in the standard macOS About panel, appended to ToDesktop's existing `tdBuildId` parens (so e.g. `Version 0.2.30 (260528yf2ma2jd4 · 06f2de0a)`). Makes shipped binaries traceable back to a commit without a side-channel mapping. Dev runs are skipped (`app.isPackaged` gate) so a stale `build-info.json` from yesterday's `pnpm build` doesn't surface in `electron .` launches.
- minds.app: pin pnpm via ToDesktop's first-class `pnpmVersion` config field instead of the home-rolled `installPnpm()` ladder. ToDesktop documents `pnpmVersion` / `nodeVersion` / `npmVersion` as build-server-provisioned versions; setting `"pnpmVersion": "10.33.4"` in `todesktop.json` is enough to keep ToDesktop's CI off `pnpm 11.1.0` (which crashes on the Linux runner's Node 20.20.0 with `ERR_UNKNOWN_BUILTIN_MODULE: node:sqlite`). Removes the four-strategy `installPnpm()` ladder (plain `npm -g`, `sudo -n npm -g`, `sudo -n curl` static binary), its three helpers, the `PNPM_VERSION` constant, and the 14-line "why pnpm 11 breaks" comment from `scripts/download-binaries.js` -- ~80 LoC out. The `beforeInstall` hook still runs but only for `uv` and `git`, which ToDesktop has no first-class knob for.
- minds.app: the packaged macOS build now actually works end to end (download -> launch -> create agent -> first message). Three bundling bugs that broke prior packaged releases are fixed:
  - `bundleClientConfig()` wrote `_bundled/{client.toml,root_name}` only into the source tree (packed into `app.asar`, which the Python backend cannot read). The packaged runtime resolves the bundle under `Resources/pyproject/imbue/minds/config/envs/_bundled/`, so `build.js` now stages a copy there; without it the backend exited with "No client config file is set" before emitting a login URL.
  - The bundled `git` was the macOS xcode-select shim (118 KB), not a runnable binary -- `git clone` of the template repo died with SIGKILL. `build.js` now bundles the real git binary plus its `libexec/git-core` helpers (shared with `download-binaries.js`).
  - Raised ToDesktop `uploadSizeLimit` to 600 MB so the larger bundle (real git) can upload.
- minds.app: drop `--reuse --update` from the create-form's non-IMBUE_CLOUD path (`agent_creator.py`). When the user creates a fresh agent from the UI, mngr's `--reuse` matches on agent name alone -- which collides with a leftover `system-services` agent on a different host and tries to update *that* one (causing git push failures on the in-VM `mindsbackup/<id>` branch). For LIMA/LOCAL/VPS_DOCKER the create flow now relies on `--new-host` to express the user's "fresh host" intent and omits the reuse flags entirely; IMBUE_CLOUD still passes `--reuse` because the baked pool host expects it. Drove this from a real failure creating `mindtest-5` and reproduced on `mindtest-6`.
- minds.app: bump to 0.2.29. Fix "Check for Updates does nothing" regression introduced in 0.2.27, this time using ToDesktop's documented API. `triggerUpdateCheck()` now branches on the **return value** of `autoUpdater.checkForUpdates()` -- per the ToDesktop runtime docs, it resolves to `{ updateInfo }` where `updateInfo` is the release metadata if a newer version exists, or null/absent when current. We show "Update X found, downloading..." or "You're up to date." accordingly. ToDesktop's default `updateReadyAction` still owns the actual download-complete restart prompt. (An intermediate attempt used `autoUpdater.once(...)` event listeners, but events are ToDesktop's granular-control path and aren't guaranteed to fire on every `checkForUpdates()` resolve -- the return-value branch is the documented, reliable pattern.)
- minds.app: bump to 0.2.27. Rip out the custom auto-update UI and fall back to ToDesktop's defaults. The previous design suppressed ToDesktop's built-in "Restart to update" prompt (`updateReadyAction: { showInstallAndRestartPrompt: 'never', showNotification: 'never' }`) and tried to replace it with a titlebar "Update" pill -- but the pill's renderer half was never wired up (no `chrome.js` consumer of the `update-ready` / `installUpdate` IPC), so users got update *detection* with no way to *install*. Now `todesktop.init()` runs with no overrides: ToDesktop checks on launch + interval, downloads in the background, and shows its own restart prompt when ready. `Check for Updates...` just calls `autoUpdater.checkForUpdates()` and lets ToDesktop drive the UI. Removed the dead `is-update-ready` / `install-update` IPC handlers, the `update-downloaded` listener, the `updateReady` flag, and the `isUpdateReady` / `onUpdateReady` / `installUpdate` preload exports. Note: still only works for **released** builds -- `@todesktop/runtime` leaves `autoUpdater` null on draft builds.
- minds.app: bump to 0.2.26. Apply `apps/minds/patches/latchkey@2.10.1.patch` to the staging `node_modules/latchkey` in `scripts/build.js` after the `npm install` step. `pnpm patch` registers the patch in `pnpm-workspace.yaml::patchedDependencies` and applies it during pnpm-managed installs, but the bundling step uses plain `npm install` (chosen because it works without pnpm's nested-modules layout for the asar packager), and npm does not honor pnpm's patch metadata. So the workspace had a patched latchkey but the shipped binary had a vanilla one, and end users on 0.2.25 still hit the `response.text: Network.getResponseBody` crash that PR #64 fixes. The bundled binary's `~/Applications/minds.app/Contents/Resources/latchkey/node_modules/latchkey/dist/src/services/google/base.js` now actually contains the catch.
- minds.app: bump to 0.2.25. `electron/env-setup.js` now passes `--reinstall-package` for every workspace wheel we ship (minds, imbue-mngr, imbue-mngr-claude, imbue-mngr-forward, imbue-mngr-imbue-cloud, imbue-mngr-lima, imbue-mngr-modal, imbue-common, concurrency-group, resource-guards, modal-proxy). The bundled wheel filenames keep the same PEP 440 version (`minds-0.1.0`) across releases, so without this hint `uv sync` considers them already-installed and skips updating them on upgrade -- the user keeps running the OLD code in `~/.minds/.venv` even after the new `.app` bundle has been swapped in. Adds ~2-5s to launch (workspace wheels re-extract every time; PyPI deps stay cached). Caught in 0.2.24 testing: a fresh agent kept hitting the pre-strip "Authorization failed / requires preparation first" code path because the new permissions.py wheel never reached the venv. Workaround for 0.2.24 users: `rm -rf ~/.minds/.venv` and relaunch.
- minds.app: bump to 0.2.24. Permission dialog now grants scope only -- it no longer runs `latchkey auth browser` / `auth browser-prepare` host-side. Credential acquisition is driven by the agent itself via the gateway's `/latchkey/` RPC, which is gated by the gateway password only (no per-scope check); the host pops the Chrome sign-in window on demand when the agent next hits a 401. This removes ~150 lines of orchestration from `desktop_client/latchkey/permissions.py`, kills the substring-match against latchkey error copy, drops the DENIED-on-auth-fail path, and makes credential acquisition retry naturally in the agent loop instead of dead-ending the dialog. Empirically verified end-to-end on a fresh agent + clean Gmail state today: agent ran prepare -> browser -> Gmail list/get-message via the gateway successfully.
- minds.app: vendor a local copy of latchkey's PR #64 fix (catch `Network.getResponseBody` race in `checkGoogleLoginResponse`) via `pnpm patch latchkey@2.10.1`. Without this the gateway crashes mid-prepare-flow on macOS (unhandled rejection from `response.text()` when the body is disposed during navigation), which leaves the SSH reverse tunnels pointing at a dead port and breaks all subsequent agent-driven `auth` calls. Patch lives in `apps/minds/patches/latchkey@2.10.1.patch` and is registered in `apps/minds/pnpm-workspace.yaml::patchedDependencies`. Remove when the upstream PR merges and we bump.
- minds.app: bump to 0.2.23. Raises `workspace_ready_timeout_seconds` from 60s to 300s (`agent_creator.py`). On a fresh Lima VM, first-boot provisioning (`uv sync`, `npm ci`, `npm run build` for the system_interface frontend) routinely takes 90-180s, so the 60s default was causing minds.app to time out the readiness probe and publish the redirect into a still-booting agent -- the chat panel then showed 'Backend not yet available, Retrying...' for the rest of the user's session even though the agent was healthy seconds later. The probe is cheap; a generous cap is harmless.
- minds.app: bump to 0.2.22 -- pin latchkey to ^2.10.1 (latchkey PR #67 merged, released as 2.10.1 at 2026-05-12 15:21 UTC). Lockfile refreshed to pick it up; no other changes since 0.2.21.
- minds.app: bump to 0.2.21 -- make the pnpm-install hook robust on ToDesktop's Linux runner. v0.2.20's `npm install -g pnpm@10.33.4` failed there with "Command failed" (no stderr surfaced -- node's execSync inherit-stdio doesn't propagate to ToDesktop's CI log). Likely EACCES on /usr/lib/node_modules without root. New strategy in `scripts/download-binaries.js`: (1) try plain `npm install -g`, (2) on failure try `sudo -n npm install -g` (Azure DevOps hosted runners have passwordless sudo), (3) last resort `sudo -n curl` the static pnpm binary from GitHub releases directly into /usr/local/bin. Each strategy's stderr is now captured and printed. Mac keeps working via strategy 1; Linux should succeed at strategy 2 or 3.
- minds.app: bump to 0.2.20 -- pin pnpm to 10.33.4 (the version `@latest` resolved to during our last green ToDesktop builds on 2026-05-06) by installing it globally in the `todesktop:beforeInstall` hook. ToDesktop's CI does a `pnpm --version` check before running `npx pnpm@latest`, so a globally-installed pnpm wins. This unblocks BOTH platforms: pnpm 11.1.0 (current `@latest`) requires Node >=22.13 and `require`s `node:sqlite` (Node >=22.5), which crashes ToDesktop's Azure Linux runner (Node 20.20.0); and 11.1.0's strict-builds is a hard exit even with `allowBuilds` configured. Pnpm 10.33.4 has no Node-22 requirement, doesn't use node:sqlite, and only warns (not errors) on unapproved build scripts.
- minds.app: bump to 0.2.19 -- structural fix for ToDesktop bundling. Adds `nodeLinker: hoisted` to `pnpm-workspace.yaml` so pnpm materialises every transitive dep at top-level `node_modules/` (the only place ToDesktop's asar packager looks). Without it, transitive deps live only at `node_modules/.pnpm/<pkg>@<ver>/node_modules/<pkg>/`; Node runtime resolution finds them via symlinks, but the packager walks top-level only and silently drops them. v0.2.17 built cleanly but crashed at launch on `electron-updater`; v0.2.18 would have crashed on the *next* missing dep (`del`, `execa`, etc.). Drops the now-redundant direct `electron-updater` declaration.
- minds.app: also adds `scripts/preflight.sh` that runs ToDesktop's exact pnpm install command locally (with @todesktop/cli stripped to match what their `postProcessApplicationSource` step does) and verifies every `require()` in our code + `@todesktop/runtime`'s code is reachable at top-level node_modules. Catches both classes of bug (`ERR_PNPM_IGNORED_BUILDS`, missing transitives) before burning a remote build cycle. Run with `bash scripts/preflight.sh` from `apps/minds/` or repo root.
- minds.app: bump to 0.2.18 -- declare `electron-updater@^4.6.1` as a direct dep so it gets hoisted into top-level `node_modules/` and ends up inside `app.asar`. `@todesktop/runtime@1.6.4` requires `electron-updater` as a regular dep, but with pnpm 11's nested-modules layout it lands at `node_modules/.pnpm/electron-updater@.../node_modules/electron-updater/` -- and ToDesktop's bundler doesn't follow that indirection, so the packaged app crashed on launch with `Cannot find module 'electron-updater'` (required from `app.asar/node_modules/@todesktop/runtime/dist/autoUpdater/AutoUpdater.js`). Declaring it ourselves forces pnpm to hoist it to the visible top-level path the packager actually copies.
- minds.app: bump to 0.2.17 -- approve electron's postinstall via `pnpm-workspace.yaml`'s `allowBuilds` so pnpm 11.1.0 stops exiting 1 with `ERR_PNPM_IGNORED_BUILDS` in ToDesktop CI. v0.2.16's `pnpm.ignoredBuiltDependencies` field in package.json was a dead end -- pnpm 11.1.0 looks at `allowBuilds` (the format `pnpm approve-builds` writes), not the older `onlyBuiltDependencies` / `ignoredBuiltDependencies` keys. Reproduced ToDesktop's exact `npx pnpm@11.1.0 install --prod=false --no-frozen-lockfile` locally; exit 1 in every other config, exit 0 with `allowBuilds: {electron: true}`.
- minds.app: bump to 0.2.14; merge latest origin/main (297→3 merge cycles, AIProvider enum, mngr/list refactor, LaunchMode.DEV removal); default agent branch back to `pilot` (after FCT `pilot` rebased on FCT main + uv tool install -e fix for the imbue-mngr editable/non-editable conflict). End-to-end verified: dev-mode + FCT pilot creates an agent, welcome auto-fires, first `mngr message` round-trips ("7×8?" → "56").
- minds.app: revert `libs/modal_proxy/pyproject.toml` only-include workaround (main removed the runtime import of `modal_proxy.testing` from `mngr_modal/backend.py`, so testing.py no longer needs to ship in the wheel).
- minds.app: bump to 0.2.13; package the new mngr_forward and mngr_imbue_cloud workspace plugins. After main's rearchitecture, `minds run` spawns `mngr forward` as a subprocess but the desktop build never bundled the imbue-mngr-forward / imbue-mngr-imbue-cloud wheels. v0.2.12 launched but `mngr forward` exited with `Error: No such command 'forward'` because the plugin wasn't installed. Fix: add both packages to apps/minds/scripts/build.js WORKSPACE_PACKAGES and apps/minds/electron/pyproject/pyproject.toml dependencies + sources.
- minds.app: bump version to 0.2.12; modal_proxy/pyproject switches to only-include whitelist so modal_proxy/testing.py (TestingModalInterface, a runtime export imported by mngr_modal/backend.py) ships in the wheel. v0.2.11 packaged build crashed on launch with ModuleNotFoundError: No module named 'imbue.modal_proxy.testing' because main's unified `**/testing.py` wheel-exclude rule stripped it.
- minds.app: bump version to 0.2.11; merge latest origin/main bringing the mngr_forward/mngr_imbue_cloud rearchitecture (single mngr forward subprocess via EnvelopeStreamConsumer in place of MngrStreamManager + per-agent mngr event followers)
- minds.app: reject `--port 0` / `--mngr-forward-port 0` for `minds run` with a clear UsageError instead of letting mngr_forward crash later on `--reverse 0:0`
- minds.app: kill orphan `mngr event` subprocesses before starting fresh stream, fixing "Workspace server not yet available" when prior backend exits uncleanly (legacy MngrStreamManager path; superseded by main's rearchitecture but preserved in v0.2.10)
- minds.app: bump version to 0.2.10
- minds.app: pyproject declares psycopg2-binary so packaged build matches dev workspace
- minds.app: env-setup and backend pass `--active` to `uv` so the venv lives in user-writable space (~/.minds/.venv) instead of the read-only signed bundle
- minds.app CI verify: per-phase wall-clock instrumentation in `apps/minds/scripts/launch_to_msg_e2e.py`. The /api/create-agent/<id>/status poll loop now monotonic-times each phase transition (CLONING_REPO -> CHECKING_OUT_BRANCH -> CREATING_WORKSPACE -> WAITING_FOR_READY -> DONE) and emits a summary line at DONE plus a `launch-to-msg-timings.json` artifact (lands inside `/tmp/launch-to-msg-screenshots/` so the existing collect-screenshots step picks it up). Also adds `wipe_lima_caches` workflow_dispatch input that flips `mac-runner-reset.sh` from warm mode (preserves `~/Library/Caches/lima/download`) to cold mode (nukes `~/.lima`, `~/Library/Caches/lima`, `~/.minds/template-cache`, `/tmp/minds-clone-*`). Together: lets us A/B cold vs warm by toggling the dispatch input, and gives the per-phase numbers needed to track speedup work over time.
- minds.app CI verify: harden the slack-flow approval round-trip. Lowercase the agent-message substring check so a "Waiting for your approval" phrasing matches the same set as "awaiting/wait for"; broaden the patterns to `permission request / requested read / approval / approve`. Re-resolve the chat panel page on each iteration of the post-approval poll loop -- after Approve, Electron sometimes z-orders the Projects page on top so `win` ends up pointing at it; `find_chat_window(ctx)` swaps back. Bump `DRIVE_SLACK_TIMEOUT` 240 -> 360s (Claude tail latency after the gateway is approved can spike); on timeout, dump every page's URL + first 200 chars of body so future-me can tell whether the chat panel was alive somewhere.
- minds.app CI verify: dismiss + prevent macOS screensaver during the run. Launching `caffeinate -dimsu` from the e2e script's `amain()` asserts UserActivity, which wakes a screensaver-locked display the same way moving the mouse does, and the `-d -i -m -s` flags keep the display + system + disk awake for the run; `killall ScreenSaverEngine` runs immediately before each `screencapture -x` as belt-and-braces in case the screensaver re-engaged between caffeinate assertions. Without this the runner's whole-desktop shots were pure wallpaper-only PNGs (no menubar, no Dock, no app). Same pattern Apple's xcodebuild CI tooling and the GitHub-hosted macOS runners use. Caffeinate proc is terminated at end-of-script so the runner returns to its normal idle policy when no run is active.
- minds.app CI verify: switch publish step to make per-window Playwright screenshots (`.win.png`) the headline shots in the side `ci-screenshots` branch, with screencapture-`-x` outputs demoted to `.desktop.png` forensic dupes; embed-in-summary step skips `.desktop.png`. The Playwright shots capture the actual rendered Electron UI (chat tabs, message bubbles, tool calls, slack-mock PASS marker) and are display-state-independent; the whole-desktop shots add no value to the job summary. Also include the 00-04 prefixes in the publish loop (previously silently dropped) and wipe `SCREENSHOT_DIR` at the start of every e2e run so stale `99-create-timeout-*` from a past run can't be re-published into this run's per-run_id side-branch dir.
- minds.app CI verify: consolidate launch-and-verify.sh + first-message-verify.sh + slack-mock-setup.sh + slack-mock-teardown.sh + drive-slack-ci.js + slack-mock-server.js into one Python script `apps/minds/scripts/launch_to_msg_e2e.py` driven by the verify job via `uv run --package minds python ...`. CDP-attach: subprocess.Popen the Minds binary with `--remote-debugging-port=N`, `chromium.connect_over_cdp()` since playwright-python has no `_electron.launch`. Drives create-agent via `page.evaluate("fetch('/api/create-agent', ...)")` with explicit `launch_mode=LIMA` rather than the form (the prod-tier form defaults to compute=DOCKER without an Imbue Cloud account, which a vanilla mac runner can't provision); polls `/api/create-agent/<id>/status`; on DONE navigates to home and clicks the workspace tile to open the chat panel; sends the first message and waits for the pong reply. Slack flow uses an in-process stdlib http.server mock on :8443 with `sudo socat OPENSSL-LISTEN:443 ... TCP:127.0.0.1:8443` for TLS termination, /etc/hosts patched, latchkey slack creds pre-seeded via the bundled `latchkey auth set slack` shim with explicit `LATCHKEY_ENCRYPTION_KEY` env var.
- minds.app: bump to 0.2.34. Checkpoint build that pairs with the parallelized FCT pilot (three FCT-side optimizations landed: combined apt installs, parallel extra_provision_command via base64+bash, pre-built system_interface frontend committed instead of npm-ci+npm-run-built per agent). Cumulative measured cold-cache CI cut: 360.7s (0.2.33) -> ~270s (0.2.34) on the same self-hosted mac runner. No apps/minds code change vs 0.2.33; the bump is a version checkpoint so a single ToDesktop bundle ID can be cited as "the version that ships with the parallelized FCT". FCT pilot commits in this cycle: b90a40f6f (combine apt+drop ttyd), 716e6f2c6+d8ecd46ba+21c60e6fd+ed1666c8b (parallelize, four iterations to land base64-encoded bash script that survives pyinfra+SSH+dash transport), b89e169c6 (commit pre-built frontend static/).
- minds.app CI verify: trust slack-mock self-signed cert in `/Library/Keychains/System.keychain` so latchkey's bundled `services info` curl (SecureTransport, ignores `CURL_CA_BUNDLE`) and the auth-browser Chrome navigation accept it during TLS handshake. Without trust, services_info reported INVALID → grant() ran auth_browser → no human → request DENIED with no slack-api rule written, and the agent's retries hit "Request not permitted by the user." The cert is now regenerated each run (so trust matches what socat serves) and removed in teardown. Also reword the post-approval kick message: dropped the `TOK <NONCE>: <message>` prefix request, which had a verbatim-echo-behind-marker shape that Claude was refusing as a prompt-injection / exfiltration probe ("That prefix request is unusual...") seen on CI run 26903006387 in `99-TIMEOUT-no-canned-body.win.png`.
- minds.app CI verify: replace the keychain-trust approach (commit 63fdd5394) with `LATCHKEY_CURL` + a combined CA bundle. The runner has no `NOPASSWD: sudo security`, so `sudo security add-trusted-cert` blocked on a hidden password prompt and the 40-minute job timeout fired with nothing past "trusting slack-mock cert..." in e2e-stdout.log (run 26904472637). Latchkey's config.ts reads `LATCHKEY_CURL` for the curl binary path -- point it at brew curl (OpenSSL build, honors `CURL_CA_BUNDLE`) so `checkApiCredentials` never falls back to system curl (SecureTransport, ignores `CURL_CA_BUNDLE`). `CURL_CA_BUNDLE` now points at a fresh file that concatenates the self-signed slack cert + `/etc/ssl/cert.pem`, so non-slack curl calls keep working alongside the /etc/hosts-mapped slack.com hits. No sudo needed, no GUI prompt.
- minds.app build: replace `bundleLatchkey()`'s `npm install --no-package-lock` scratch dir with `pnpm --filter minds deploy --prod --config.node-linker=hoisted --config.ignore-scripts=true --config.inject-workspace-packages=true`, so the shipped `resources/latchkey/node_modules/` tree is pinned by `apps/minds/pnpm-lock.yaml` instead of fresh-resolving every latchkey transitive at build time. The old path floated `playwright` / `playwright-core` independently of the lockfile and already shipped a broken combination (1.60.0 internals against latchkey code expecting pre-1.60); upstream stopgap is latchkey PR #81's `~1.60.0` self-pin, this is the durable fix. Cross-platform native prebuilds (`@napi-rs/keyring-*`, playwright fsevents) now come in via `supportedArchitectures` in `pnpm-workspace.yaml` (all 8 keyring variants vs the host-only one before). Added drift-guard tests `test_bundle_latchkey_uses_pnpm_deploy_against_lockfile` and `test_pnpm_workspace_pins_cross_platform_architectures` so a future revert to npm-install or removal of `supportedArchitectures` fails fast. Bundle size: 45M (vs 50M before). Smoke test (`cli.js --version`) preserved; `.bin/*` symlinks materialized by the existing `dereferenceSymlinksInPlace()` pass; verified locally that the deployed tree contains playwright@1.60.0 + playwright-core@1.60.0 + latchkey@2.15.0 + 8 keyring prebuilds + 0 chromium binaries + 0 external symlinks.
- minds.app: dev binary and tests now use the bundled restic in `apps/minds/resources/restic/restic` -- no more "you need to brew install restic" for end users or devs. `electron/paths.js::getResticPath()` drops the `if (isDev()) return null` workaround and returns the bundled path in both modes; backend.js plumbs that to `MINDS_RESTIC_BINARY`, which `desktop_client/restic_cli.py` now reads lazily at every callsite (was a module-level constant). `package.json` adds a `prestart` hook that runs new `scripts/ensure-binaries.js` -- a lazy wrapper around `download-binaries.js` that no-ops when `resources/{restic,uv,git,lima}` are all present, so subsequent `pnpm start` invocations don't pay 30MB of re-download. Tests get the env via `apps/minds/conftest.py`: when the bundled binary exists, `MINDS_RESTIC_BINARY` is set before any test module imports. `desktop_client/testing.py::restic_backup_a_file` and `backup_status_test.py::_backup_a_file` had hardcoded `["restic", ...]` invocations -- swapped to `[_get_restic_binary(), ...]` so they honor the env too. Verified locally with `brew uninstall restic`: 1030 tests pass, the 5 still-failing webdav tests are macOS `/private/var/folders` path-resolution issues pre-existing on origin/main (not introduced here).
- minds.app CI verify: add `pre_run_sweep()` at the top of `launch_to_msg_e2e.py::amain()` so the self-hosted Mac runner is reset to a known-clean state before every run. Pairs with the existing teardown blocks (which handle the success path): mid-run crashes that skip teardown now have their residue wiped on the next run's startup, so runs stay reproducible without manual runner janitoring. Sweeps: stale `/etc/hosts` slack-mock line, stale `sudo socat OPENSSL-LISTEN:443`, orphan Minds.app + `mngr forward` + `mngr event` children (`mngr event` and `mngr forward` killed first so the Electron parent's children exit cleanly), orphan `caffeinate -dimsu`, /tmp scratch dirs (`/tmp/slack-mock/`, `/tmp/launch-to-msg-screenshots/`, `/tmp/minds-electron.log`), and stale latchkey slack creds. Kill paths use `pgrep -lf` + targeted `kill <pid>` per CLAUDE.md (never `pkill -f` with broad patterns).
- minds.app: bump to 0.2.35 to cut a ToDesktop bundle that actually ships the build.js + restic + ratchet changes from this branch. The prior CI runs on this branch all green-verified the *workflow* (it always downloaded the latest released 0.2.34 binary), not the new build.js: 0.2.34 was built before pnpm-deploy landed, so resources/latchkey/ inside that binary still came from the old `npm install --no-package-lock` scratch dir. 0.2.35 is the first binary that exercises the pnpm-deploy path end-to-end and the first to ship `MINDS_RESTIC_BINARY` plumbed in both dev + packaged mode.
- minds.app CI: `minds-launch-to-msg.yml` makes `commit_sha` required (was optional with empty default). Previously, dispatching with no inputs silently fell through to `verify` downloading the latest released bundle from ToDesktop's update feed, so the workflow would go green even though it never tested the code at HEAD. This bit us three runs in a row on this branch (b8f193ac7, 7b490eabe, 174c29674 all "green" against a stale 0.2.34 bundle). With `required: true` GitHub now refuses dispatch without a SHA; the `resolve build URL` step also drops the "latest released" fallback branch and hard-errors if both inputs are empty (defense-in-depth). When a user explicitly wants to verify a known-good bundle against new test infra, they can still pass `app_zip_url` -- now documented as the explicit escape hatch, not a defaulted-empty knob.
- minds.app CI: remove `inputs.app_zip_url` from `minds-launch-to-msg.yml` and require `inputs.template_ref`. Was: workflow had three valid input modes -- `commit_sha` (build + verify), `app_zip_url` (skip build + verify URL), or empty-both (skip build + verify the released bundle). Now: only one mode -- build always runs, looking up an existing ToDesktop build by `versionControlInfo.commitId == commit_sha` and reusing if found, packaging fresh otherwise. Verify always consumes the artifact this build produced. `template_ref` becomes required and gets resolved up-front to a full FCT git SHA (via `git ls-remote`) that's surfaced in the run summary and passed to the e2e script. The agent runtime is a function of (minds binary, FCT template), so pinning only the minds side left the same reproducibility trap on the FCT side. Both git SHAs + the ToDesktop build_id now appear in `Verifying` / `FCT template` summary blocks; if either is missing, the run is not claim-quality.
- minds.app CI verify script: `launch_to_msg_e2e.py` gains `MINDS_AI_PROVIDER` env knob (default `API_KEY`, accepts `SUBSCRIPTION`) so the same script can drive both CI runs (which need an explicit API key on the form) and local dev drive-tests (which use the user's already-synced Claude.ai credential). In SUBSCRIPTION mode the api-key fill is skipped entirely; no key needs to be present in the environment. Also tightens `pre_run_sweep`'s stale-Minds.app pgrep: previously `pgrep -lf "/Applications/Minds.app/..."` matched any process whose argv contained that literal string -- including shell command lines that just mentioned it, e.g. the script's own subprocess wrappers. Now the kill loop verifies via `ps -p <pid> -o command=` that argv[0] actually starts with the expected absolute path before sending SIGTERM, so the sweep cannot kill the wrong process.
- minds.app CI: bump `build` job's `timeout-minutes` 40 -> 60 in `minds-launch-to-msg.yml`. Run 26935971928 stalled at "Notarizing Minds for arm64 (this may take up to 30 minutes) (35%)" -- Apple's notarization service really does take up to ~30 min per arch in the worst case, and we sign+notarize both x64 and arm64 sequentially. 40 min was insufficient padding for that worst case; 60 min absorbs it without making the post-failure cancel step tear down builds that just needed a few more minutes.
- mngr_claude: drop the inline `_bridge_credentials_to_default_claude_home` symlink (was PR #1869's approach). Empirically proved unnecessary on the pilot path: with the helper removed (commit `a28f5a146`) and FCT pilot's existing Phase D bash cred-watcher in `extra_provision_command` doing the symlink, subscription-mode chat round-trips authenticate without 401 on BOTH the CI Mac runner (run 26980729677, build_id 260604gircvwscg, all 13 screenshots green + slack PASS + 127s create) AND a local SUBSCRIPTION-mode drive (Welcome → pong reply visible in 06; `/tmp/cred-bridge.log` inside the Lima VM confirms FCT pilot's watcher fired). Josh's review on #1869 was right — for the Lima+pilot path this branch ships, the symlink belongs in the FCT template's `extra_provision_command` where it already lives, not in `mngr_claude.plugin._setup_per_agent_config_dir`. Branch-side delta: 3 unit tests + the helper + the call site removed (198/198 mngr_claude tests pass).
- Cleanup sweep on top of the 0.2.35 baseline. Code removed: a defensive `port <= 0` guard and its unit test in `imbue/minds/cli/run.py` (redundant with mngr_forward's own check); two personal/local-only files dropped from the index via `.local.sh` / `.local.md` rename (drive-minds, revive-agent-chat, the minds-ops slash command). Linux-compat: `electron/backend.js`'s PATH-append comment no longer mis-claims `/usr/local/bin` is Mac-only. Docs accuracy: `docs/desktop-app.md` stops claiming a non-existent AppImage Linux target; `docs/release.md` runbook added describing the macOS arm64 release procedure. Supply-chain hardening: `pnpm-workspace.yaml` `minimumReleaseAge` + main's exempt list restored; `electron/pyproject/pyproject.toml` `[tool.uv] exclude-newer = "14 days"` restored. Test-only support: e2e fixture `pickContentWindow` helper added so launch-smoke can screenshot the content view rather than the chrome strip; spec renamed `launch-smoke.spec.js` → `macos-launch.spec.js` (the path already conveys minds + e2e). Empirical validation: each change ran through ci.yml + minds-launch-to-msg.yml on a candidate branch before landing.
- minds.app: bump to 0.2.36. Cuts a ToDesktop bundle on top of the simplification sweep (~2.5k LoC net removed since 0.2.35) with no behavior change for end users. FCT template pin (`FALLBACK_BRANCH`) unchanged at `v0.2.35` (fb96b1b3); the cleanup is mngr-side only. Verified via `minds-launch-to-msg.yml` × FCT `v0.2.35`.
- minds.app: refresh `_build_mngr_create_command`'s docstring to match the actual code path (only IMBUE_CLOUD passes `--reuse`; non-IMBUE_CLOUD modes rely on `--new-host` for fresh-host intent). Parametrize the no-reuse / new-host assertion in `agent_creator_test.py` across DOCKER, LIMA, CLOUD so the Lima path's invariant is asserted explicitly. This locks the contract that minds.app's create-form does not depend on mngr-side PRs #1694 / #1720, which fix the `--reuse` host-scope matching: since the create-form path never passes `--reuse` for these modes, the wrong-host-scope-match bug those PRs address cannot reach a minds.app user. Drops a stylistic leftover (`base_branch_name` two-statement split with intermediate `head_name`) from `libs/mngr/imbue/mngr/hosts/host.py` so `libs/mngr/imbue/` is byte-identical to main.
- minds.app CI: drive a second workspace (`HOST_NAME_2`, default `${HOST_NAME}-b`) through the same launch_to_msg_e2e.py run on the self-hosted mac runner, then send cross-workspace follow-up pings to BOTH chat URLs to prove each agent stays responsive after the chat BrowserWindow re-navigates. `WORKSPACE_COUNT=2` enables the multi-workspace mode in CI; `=1` (default) preserves single-workspace local-repro behavior. Refactors create+first-message into a `_create_workspace_and_first_message` helper and adds `_send_followup_and_verify` for the cross-workspace pings; screenshots `09-16` cover W2 create / first-msg / cross-workspace pings (W1 stays at `03-06`; slack at `07-08`).
- minds.app CI: chain four additional state-transition checks onto the same single-runner session for maximum depth coverage. After the cross-workspace pings: (a) navigate to `/` and assert BOTH workspace tiles render (screenshot 17, manually verified to show `e2e<HHMMSS>` "Created 6m ago" + `e2e<HHMMSS>-b` "Created 2m ago"); (b) POST `/api/destroy-agent/<W2-agent-id>` (parsed out of W2's chat URL via `agent-([a-f0-9]+)\.localhost`), poll `/api/destroying/<id>/status` until done or 404, then reload `/` and assert W1's tile stays while W2's is gone (screenshot 20); (c) send a `bink`-token follow-up to W1's chat to prove W1 stays responsive after the destroy (screenshots 21-22); (d) run the bundled `mngr list --format json --quiet --on-error continue` against `MNGR_HOST_DIR=$HOME/.minds/mngr` and assert HOST_NAME is in the agent set while HOST_NAME_2 is absent -- cross-checks the destroy lifecycle against mngr's canonical state from a different angle than the UI's discovery cache; PATH is augmented with `/Applications/Minds.app/Contents/Resources/lima/bin` so the lima provider can find `limactl`; (e) POST `/api/create-agent` with HOST_NAME (already owned by W1) and assert 409 + "already exists" in the body, exercising the duplicate-name guard added to `_handle_create_agent_api`. `mac-runner-reset.sh` gains `sudo tmutil deletelocalsnapshots /` so accumulated Time Machine snapshots (each Lima diffdisk that gets destroyed leaves up to 100GB pinned behind a snapshot) don't fill the runner's home partition between runs.

- minds.app: `test_create_local_docker_workspace_via_electron` no longer mutates real `.mngr/settings.toml` files to satisfy mngr's pytest config guard. The prior version flipped `is_allowed_in_pytest` in the repo root's committed `.mngr/settings.toml` and in the FCT checkout's `.mngr/settings.toml` (the operator's `.external_worktrees/forever-claude-template/` when present), restoring both in a `finally`. That was dangerous (a crash mid-test leaves the committed guard flag disabled for the developer's later runs) and incomplete (it patched only the project layer, so a developer's untracked `.mngr/settings.local.toml` -- or any user-scope config -- still tripped the guard; the test could only pass on a pristine CI checkout). The host-side `mngr` (the app's ~1s `auth list` account poll, `mngr forward`, and the proxy's agent discovery) and `mngr create` need *different* configs and are differentiated only by cwd, so the fix adds a thin `host_config_dir` seam to `e2e_workspace_runner.py`: the Electron process runs from an opted-in copy of the repo's `.mngr` (`_isolated_host_config_root`), `mngr create` mirrors a throwaway FCT clone carrying its own opt-in (`materialize_isolated_fct` clones the external worktree rather than writing into it, which also stops `mngr create`'s in-source `git checkout` from touching the operator's checkout), and `mngr destroy` reads the host copy via `MNGR_PROJECT_CONFIG_DIR`. No real file is written, so the repo and any operator FCT worktree stay pristine even if the run is killed. Verified end-to-end against a local Docker workspace; a pure test-file `MNGR_PROJECT_CONFIG_DIR` variant was rejected because forcing the host and create paths to share one config breaks host-side agent discovery.

## 2026-06-09

- The titlebar now paints the active workspace's accent color across its full width (was: a small swatch next to the page title) and the workspace content below floats inside a 4px inset frame with 12px rounded corners, so the accent reads as a colored frame around the content.
- Title text, navigation icons, and the account button on the titlebar flip between dark and light foreground based on the accent's lightness, so future user-chosen accent colors (including dark ones) remain legible. The close button keeps its red destructive hover; the requests-badge red dot stays red.
- The most-recently-opened workspace's accent persists per window across navigation to Home (each window's bar only changes when *that window* opens a different workspace), survives app restarts, and is cleared when the stored workspace is deleted (matching windows only) or the user signs out of their account (all windows). Stored per-entry in `~/.minds/window-state.json` (existing file extended from a bare array to an object).
- Per-workspace accents are now `oklch(85% 0.08 <hue>)` (was `oklch(65% 0.15 <hue>)`); the same value powers the sidebar item spines and other accent affordances so the whole accent system stays in step. The redundant 3px top stripe on inner workspace pages is removed.

Fix the requests panel X button being unclickable when the panel auto-opens at startup. The modal opened before chrome.js had registered its `onModalStateChanged` listener, so the initial `modal-state-changed: { open: true }` IPC was dropped, the `modal-open` body class never got applied, and the titlebar's drag region intercepted the click. The chrome view's startup state-priming step (`primeViewWithCachedChromeState`, called from `did-finish-load`) now also replays the current modal-open state, alongside the cached workspaces/auth/requests state it already primed.

The startup loading window no longer flashes at the default centered
position before jumping to its saved location. Saved bounds from the
previous session are now applied to the initial window before its
loading screen renders, so the loading view appears in place and no
visible jump occurs when content loads.

Window state is now persisted in most-recently-focused order, so for
multi-window users the loading screen opens at the bounds of the last
window they interacted with (rather than the oldest still-open one).
The lesser-MRU windows are restored without stealing keyboard focus,
and the most-recently-focused window is re-raised as each restored
window appears so it stays on top in the window stack as well.

Added a `FAILED` outcome to the latchkey permission-grant flow. Previously, if
the browser sign-in (including the one-off `latchkey auth browser-prepare` step)
failed when a user approved a permission request, the request was auto-denied:
the agent was told its request was "denied" and the request was removed from the
pending inbox. Now a failed approval is reported as `FAILED` instead: the request
stays pending (no response event is written, the agent is not notified), and the
desktop dialog shows the failure reason so the user can click Approve again to
retry. Denials remain a separate, explicit user action.

Fixed a bug that broke WebDAV file sharing for macOS users. The `/api/v1/files`
WebDAV server shares the user's home directory, but on macOS that path
(`/Users/<name>`) contains uppercase characters. WsgiDAV matches request paths
against a lowercased copy of each share key yet looks the matched share back up
by that lowercased string, so any share key with uppercase characters resolved
to no provider and every request under it returned `404 Not Found: Could not
find resource provider`. The share is now registered under a lowercased key
(while the filesystem provider keeps the real, correct-case path), so home-
directory paths under macOS resolve correctly. Linux users were unaffected
because `/home/<name>` and `/tmp` are already lowercase.

Added the ability to change the shared path in the file-sharing permission
dialog before approving. The agent-requested path is now shown in an editable
field; you can paste a different absolute path or pick one with new
"Choose file…" / "Choose folder…" buttons that open a native OS file dialog
(separate file and folder pickers because a single combined picker can't select
both on Linux/Windows). Approving with an
edited path retargets the grant to your chosen path -- the access mode the agent
asked for (read-only vs. read & write) is preserved, and the edited path is
re-validated for traversal before any grant is written. The buttons appear only
in the desktop app (they use a native picker); in a plain browser you can still
paste a path.

The edited path is also validated against the WebDAV mount roots (your home
directory and the system temp directory) directly in Minds, so a path outside
those is rejected immediately with a clear message instead of being forwarded to
the gateway. The dialog gives instant feedback too: Approve stays disabled (and a
hint appears) while the path field is empty or points outside a shared folder, as
you type or pick.

# e2e: detect the CI branch so the FCT branch-matching step fires

The Electron e2e workspace runner pairs the current mngr branch with a
same-named forever-claude-template branch (`resolve_fct_path` step 2), falling
back to FCT `main` otherwise. In CI the checkout is a detached HEAD, so
`git rev-parse --abbrev-ref HEAD` returned `HEAD` and the branch-matching step
never fired -- a PR that changes the mngr<->FCT config contract could only ever
be tested against FCT `main`. `_current_mngr_branch` now consults GitHub
Actions' `GITHUB_HEAD_REF` (PR source branch) / `GITHUB_REF_NAME` (push branch,
ignoring `<n>/merge` refs) before the git fallback, so the FCT branch matching
works in CI. Other PRs are unaffected (they have no matching FCT branch and
still use FCT `main`).

## 2026-06-08

Fix `test_create_local_docker_workspace_via_electron` failing on CI (and any host without gVisor). FCT's `[providers.docker]` block now sets `docker_runtime = "runsc"` to harden the local-docker provider, but `runsc` is not installed in GitHub Actions runners, so `docker run --runtime runsc` failed with "unknown or invalid runtime name: runsc" and the workspace never reached the agent navigation URL. The test now sets `MNGR__PROVIDERS__DOCKER__DOCKER_RUNTIME=runc` via `monkeypatch.setenv` -- the exact escape hatch FCT's settings.toml comment names for CI / Modal -- which the Electron child inherits through `_build_electron_env`.

## 2026-06-08

The right-side requests panel is gone: pending permission requests now live
in an inbox modal opened from the same titlebar bell, with a master/detail
layout. Opening the inbox no longer resizes or shifts the workspace -- it
overlays the window the same way the permission dialog already did.

Approving or denying a request keeps the inbox open and auto-advances to
the next pending item. Browser-mode deep links are now ``/inbox?selected=<id>``
(the standalone ``/requests/<id>`` page has been removed).

Minds bootstrap now writes the gVisor runtime settings into each per-account
`[providers.imbue_cloud_<slug>]` block it registers: `docker_runtime = "runsc"`,
`install_gvisor_runtime = true`, and
`default_start_args = ["--workdir=/", "--security-opt=no-new-privileges"]`. This
makes the imbue_cloud slow (rebuild) path run the agent container under gVisor
with the runsc hardening args, mirroring the forever-claude-template
`[providers.ovh]` bake settings. No user-visible change to the create flow.

Added a `--no-recycle` flag to `minds pool create` that forwards `--no-recycle`
to the admin command, forcing a fresh OVH VPS order instead of reclaiming a
cancelled one (useful for testing the fresh-provision path).

Fixed two JinjaX template bugs where a component tag had a quoted attribute
containing `{{ ... }}` (which JinjaX forwards literally instead of interpolating):
the Landing page's settings-gear `<Button onclick="...{{ agent_id }}...">` (which
navigated to a literal `/workspace/{{ agent_id }}/settings` and then 500'd the
destroy with "AgentId must start with 'agent-', got '{{ agent_id }}'") and the
Sharing page's `<Link href="...{{ agent_id }}...">` (dead "open workspace" link).
Both now use the `attr={{ expr }}` form. Added render regression tests asserting
no literal `{{` survives in the Landing / Workspace-settings / Sharing pages.

Three fixes to the new-workspace creation flow:

- **Post-login redirect.** After signing in (email/password or OAuth) or finishing email verification, users now land on the new-workspace screen (`/`) when they have no workspaces yet, instead of always being dropped on the account-management page. Returning users who already have workspaces continue to land on `/accounts`. All sign-in paths funnel through a new `/post-login` endpoint that branches on the workspace count.
- **Leased-host account binding.** Workspaces running on a host leased from Imbue Cloud (provider `imbue_cloud_<account-slug>`) can no longer be disassociated or re-associated to a different account, preventing confusing "account mixing". The settings page shows the bound account with a disabled Disassociate control and an explanatory note, and the associate/disassociate backend routes reject such requests with HTTP 403. Non-leased workspaces are unaffected.
- **Region preference.** When the create page is opened, minds kicks off a best-effort, non-blocking lookup of the user's IP geolocation (via `ifconfig.co/json`) and stores the nearest OVH-US datacenter (`US-EAST-VA` or `US-WEST-OR`) as a preferred region in `~/.minds/config.toml`. IMBUE_CLOUD workspace creation passes it to `mngr create` as a soft `-b preferred_region=` hint, so a closer host is used when one is free without ever blocking the fast path. The lookup adds no page-load latency and refreshes at most about once per hour per process.

Test infra (not user-visible): made the Electron e2e workspace runner's onboarding step resilient to a Playwright click race -- it now confirms each onboarding question screen actually advanced and retries the click, since `page.click` could land before `creating.js` attached its `.js-next` handlers and silently no-op.

Final fixes to the standardized workspace-create flow:

- Region selection is now explicit. The create form always shows a "Region"
  control under advanced settings for providers that place a host in a region
  (Imbue Cloud and Vultr). It defaults to that provider's last-used region (saved
  per provider in `~/.minds/config.toml`), then a region guessed from your IP
  geolocation, then a hardcoded default (US-EAST-VA for Imbue Cloud, `ewr` for
  Vultr). The chosen region is remembered for next time on a successful create.
  The old, implicit "preferred region" behavior has been removed; geolocation is
  now fetched once at startup in the background instead of hourly.
- Backups no longer block workspace creation or get lost on slow hosts. Restic
  backup setup runs after the workspace is ready, retries for up to ~5 minutes if
  the host isn't reachable yet, and only notifies you if it ultimately fails.
- Destroyed workspaces now disappear from the workspace list, and destroying a
  workspace no longer reports a spurious "failed" once the host is actually gone.
- The onboarding "initial message" retry budget is raised from 10 minutes to 1
  hour, so the message still lands on slow-to-start workspaces (e.g. a cold lima
  create that boots a VM and builds an in-VM image) and when the user takes a
  while to finish logging in to their AI provider.

Bumped the LIMA launch-mode progress-bar duration estimate from 300s to 600s on
the workspace creation page: LIMA mode now boots a VM *and* builds the project
image inside it (the workspace runs in a Docker container in the Lima VM), so a
cold create takes longer than the old run-directly-in-the-VM path. This only
affects the creating-page animation, not any hard timeout.

Fixed the dev create-form defaults so they work on any tier, including staging
and production. The `MINDS_WORKSPACE_GIT_URL` / `_NAME` / `_BRANCH` env vars
(which point the create form at the operator's local FCT worktree) were
previously honored only on per-developer dev tiers and silently dropped on the
shared `minds` / `minds-staging` tiers -- so `just minds-start` against staging
fell back to the public GitHub FCT on `main`, and local FCT changes could never
be tested there.

The tier-based gate is replaced with an explicit opt-in: the form honors those
vars only when `MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1` is set in the same
environment. `just minds-start` and the e2e workspace runner set it; a normal
end-user `minds run` never does, so a stray `MINDS_WORKSPACE_*` left in the
operator's shell is ignored on every tier (the safety the tier gate provided,
now applied uniformly -- and dev tiers no longer honor stray vars by tier alone).
These defaults point at a local path + dev branch and only make sense for
local-compute launch modes (Lima / Docker), not IMBUE_CLOUD pool leases.

## 2026-06-06

Large pass over the desktop client's HTML templates to extract recurring inline Tailwind patterns into JinjaX primitives. The change set is mostly internal -- rendered behavior is preserved -- but a few visual tweaks ride along.

New / generalized primitives (under ``apps/minds/imbue/minds/desktop_client/templates/``):

- ``Card`` (rewritten): ``layout`` (``block`` / ``row`` / ``row-spread``), ``padding`` (``default`` / ``tight``), ``interactive``, ``tag`` (``div`` / ``a`` / ``button``), ``href``, plus JinjaX ``attrs`` passthrough for arbitrary HTML attributes. The visual shell moves into a shared ``.minds-card`` CSS class in ``tokens.css`` so JS-rendered surfaces (the Landing providers panel) reference one source of truth.
- ``CardPage`` (renamed from ``auth/AuthBase``): centered-card layout used by the auth flow + the Create workspace form. ``padding="default"`` (``p-10``, auth) or ``"form"`` (``p-6``, Create); ``max_width`` is a Tailwind utility. The Login / AuthError pages now go through this primitive instead of hand-rolling the centered card.
- ``Button`` / ``ButtonLink`` / ``ButtonSubmit``: add a ``size`` axis (``md`` default, ``lg`` for prominent block CTAs, ``icon`` for square padding). Disabled buttons fade to ``opacity-30`` (was ``opacity-50``). All three now use JinjaX ``attrs.render()`` passthrough.
- ``TitlebarButton``: new primitive for the dark title-bar window controls. ``variant="nav"`` (left-side icons) / ``"control"`` (min/max/close); ``tone="default"`` / ``"danger"`` (close button's red hover).
- ``Link``: new primitive for inline ``text-blue-600 hover:underline`` anchors. ``weight="regular"`` (default) or ``"medium"`` for the auth-flow tab-switch / back-link affordances.
- ``Select`` / ``Textarea``: new primitives sharing TextInput's focus-ring token via a new ``INPUT_BASE`` catalog global.
- ``FormLabel``: new primitive for form-field labels. ``inline=False`` (block, mb-1.5) or ``inline=True`` (sits beside its control). Prop is ``target=`` (the HTML ``for`` attribute id).
- ``Icon24`` / ``Icon12``: new primitives wrapping the 24x24 lucide stroke icons + the 12x12 title-bar chrome glyphs. Path data lives in ``ICONS_24`` / ``ICONS_12`` dicts in ``templates.py``.
- ``Notice``: drops the bespoke ``extra`` prop in favor of attrs passthrough so callers can pass ``id=``, ``class="hidden"``, ``data-*`` alongside ``variant=``.
- ``auth.OauthButton``: new primitive composing ``auth.OauthIcon`` + the brand label, picked by ``provider="google"|"github"``.
- ``Spinner``: gains ``tone="accent"`` (blue ring) for primary-action spinners; old inline ``border-blue-300 border-t-blue-600 animate-spin`` patterns migrate to ``<Spinner tone="accent">``.

Standardization sweeps:

- **Text colors**: banished ``text-zinc-600`` and ``text-zinc-100`` so each remaining shade carries one role (``zinc-900`` primary, ``zinc-700`` body, ``zinc-500`` secondary/label, ``zinc-400`` muted, ``zinc-200`` on-dark). Section labels (SectionHeader, inline ``<h2>`` labels) lift from 600 to 500; body paragraphs lift from 600 to 700; ghost button text moves from 600 to 700.
- **Corner radii**: retired bare ``rounded`` (20 sites swept to explicit ``rounded-md``) and ``rounded-2xl`` (PermissionsDialog + RequestUnavailable fold to ``rounded-xl`` so dialog chrome matches card chrome).
- **Borders**: 2 accidental ``border-zinc-300`` sites fold to canonical ``border-zinc-200``.
- **Shadows**: ``.minds-card`` baseline has no shadow; the ``interactive`` Card flag adds ``hover:shadow-sm``. Non-clickable cards (PermissionsHeader, the Latchkey permission cards, Associate) read as flat surfaces.
- **StatusBadge**: the ``warn`` variant drops its one-off border so all five variants share a uniform pill treatment.

CSS classes anchor a few JS-rendered surfaces that can't call JinjaX: ``.minds-card`` (Card shell), ``.spinner`` / ``.spinner-accent`` (Spinner), ``.code-pill`` (inline mono pill in Sharing).

A new ``apps/minds/imbue/minds/desktop_client/templates/README.md`` documents the rule ("use a primitive before reaching for inline Tailwind"), the catalog, where the shared tokens live, the visual-diff workflow, and the JinjaX gotchas the branch shook out (Python-keyword props, nested ``{# #}`` comments, literal ``<Tag>`` in docstrings, ``:attr="..."`` for component-tag dynamic attributes, ``!important`` on the ghost-Button link-style recipe).

``apps/minds/scripts/visual_diff.py``: the screenshot step now waits for Tailwind to inject its generated stylesheet before snapping (was a flat 400ms timeout that produced unstyled screenshots on slow machines or when ``tailwind.js`` was missing). The compare report's per-scenario thumbnails open a click-through lightbox: click image swaps A/B, ``←``/``→`` step between differing scenarios, ``Esc`` closes.

Visible end-user impact is small and is mostly subtle visual polish: the auth-flow CTAs gain canonical ``p-10`` padding (~2-4px shifts), the Landing project-row icon buttons darken slightly under the ghost variant, the auth pages' "Sign in"/"Back to" links pick up consistent ``font-medium`` styling, and a couple of misaligned form-control padding pairs now line up vertically. The ``Configure...`` disclosure on the Create form correctly renders at ``text-xs font-normal`` after a follow-up to add ``!important`` to the link-style recipe overrides.

## 2026-06-04

Migrate the desktop client's templates from Jinja2 macros + `{% extends %}` to JinjaX components. UI primitives (Button, Card, Notice, Spinner, TextInput, PageContainer, Opt) and layout (Base, AuthBase) are now `.jinja` components composed via `<Component>` tags. Each page is a PascalCase component under `templates/pages/` (and auth pages under `templates/auth/`). The permission-request dialog is decomposed into five components (`PermissionsDialog`, `PermissionsHeader`, `PermissionsForm`, `PermissionsManualCredentials`, `PermissionsError`). The dev styleguide page (`/_dev/styleguide`) gains examples for the new components.

No user-visible behavior changes -- HTML output stays semantically identical. Internal: `templates.py` now exposes a `CATALOG` constant in place of `JINJA_ENV`; the public `render_*` functions keep their signatures.

- Disable the Modal provider in the Electron desktop-client e2e test (`test_create_local_docker_workspace_via_electron`) by setting `MNGR__PROVIDERS__MODAL__IS_ENABLED=false` for the Electron child process. The test creates a local Docker workspace and is given no Modal credentials, so the spawned `mngr`'s provider discovery was logging a "Modal is not authorized" warning every ~10s for the whole run; disabling the provider keeps the logs clean.

Desktop app auto-update and developer-tooling fixes (extracted from the larger minds onboarding work for standalone review).

- Auto-update: packaged builds now prompt to install a downloaded update. ToDesktop's runtime defaults `showInstallAndRestartPrompt` to `"never"`, so users saw "downloading in the background..." and were never prompted again; it is now set to `"always"`. ToDesktop is only initialized in packaged builds -- in dev its constructor threw on macOS (Squirrel is not linked in the unsigned binary), so dev launches now skip it.
- Added a `Check for Updates...` item to the application menu that triggers a check and reports the result (update found / up to date / unavailable / error), with the unavailable message worded for the build type (dev vs unreleased draft).
- Added a `View` menu with `Toggle Developer Tools` (Alt+Cmd+I), zoom controls, and fullscreen. The default Electron DevTools shortcut crashed because the app uses `BaseWindow` + `WebContentsView` rather than a `BrowserWindow`.
- `MINDS_OPEN_DEVTOOLS=1` auto-opens detached DevTools on the content view at launch.
- Startup env-setup failures are now logged to the console in addition to being shown in the error window.

- Fixed `minds pool {list,create,destroy}` leaking the Neon pool DSN (which
  embeds the DB username + password) into the `Running: ...` log line whenever
  `--database-url` was passed explicitly. The DSN is now masked before the
  command is rendered for logging; the real subprocess still receives the
  unredacted value. The secret-masking logic that `mngr forward`'s
  `--preauth-cookie` redaction already used is now a shared
  `imbue.minds.utils.secret_redaction.redact_secret_flag_values` helper.

Documented why `scripts/launch-and-verify.sh` and `scripts/first-message-verify.sh` intentionally use `set -uo pipefail` (omitting `-e`): both handle errors explicitly via a `fail` helper, `PIPESTATUS`, retry loops that depend on commands exiting non-zero, and diagnostic blocks on failure. No runtime behavior changed.

The minds desktop client no longer runs a second discovery observer. Its `mngr forward` subprocess is now launched with `--observe-via-file`, so it tails the shared discovery events file written by the single `mngr observe` under `mngr latchkey forward` instead of spawning its own. Provider-set changes (enable/disable, signin/signout/OAuth) now refresh discovery solely by bouncing the detached `mngr latchkey forward` supervisor; minds no longer sends SIGHUP to `mngr forward` (its `bounce_observe` path was removed). Behavior is unchanged from the user's perspective.

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-04

Bump Latchkey version to 2.15.1. to include the playwright compatibility fix.

A workspace no longer flickers out of the minds desktop list when its provider has a transient discovery error. Minds now retains agents/hosts whose provider errored on a poll and marks the affected workspaces stale (an amber dot in the sidebar) while keeping them fully clickable; they are only removed on an explicit destroy or a later clean poll. On the same provider-set changes that already bounce minds' own `mngr forward` observe (provider enable/disable, imbue_cloud account add on signin, account removal on signout/OAuth), minds now also bounces the detached `mngr latchkey forward` supervisor so latchkey's discovery stays in lockstep without a full minds restart.

## 2026-06-03

Workspace creation failures are now surfaced clearly on the creating/onboarding page. Previously a failure only flipped a small, faint caption to "Failed: ..." while the heading still read "Setting up your workspace", the progress bar froze partway, and the rotating tips kept cycling -- making it easy to miss that creation had failed. Now a failure immediately replaces the progress UI (from whatever onboarding screen the user is on) with a prominent error state: a red "We couldn't set up your workspace" heading, the underlying error message in a red box, the rotating tips stopped, and "Back to setup" / "Home" buttons to recover. The collapsible "Show details" log remains available.

Fixed workspace creation failing when the source repository's requested branch is not the default branch.

- Cloning a remote repo for a non-default branch previously failed with `pathspec '<branch>' did not match any file(s) known to git`. The remote clone used `git clone --depth 1`, which (implying `--single-branch`) fetches only the default branch, so the requested branch's ref was never downloaded and the subsequent checkout could not find it.
- `clone_git_repo` now takes an optional `branch` and, when given, clones with `--single-branch --branch <branch>`: only that branch is fetched (still cheaper than a full clone) but its complete, non-shallow history is present. The remote create path passes the requested branch through.
- The shallow (`--depth 1`) clone is gone entirely. Besides the checkout failure, a shallow clone could not be mirror-pushed into the agent container's bare repo (`mngr create` rejects it with "shallow update not allowed"); a single-branch clone keeps the full ancestry that push requires.
- Requesting a branch that does not exist on the remote now fails cleanly at clone time rather than later at checkout.
- This generalizes (and supersedes) an earlier imbue_cloud-only fix: every launch mode reaches `mngr create`'s git-mirror push for a cloned-repo source (a git repo plus a new host always resolves to `TransferMode.GIT_MIRROR`), so a shallow clone is never safe for any mode, not just imbue_cloud. The mode-specific `_may_shallow_clone_remote_repo` helper is therefore removed in favor of always cloning a single branch non-shallow.

Tear out the unused refresh-event plumbing from `minds.desktop_client`:

- Drop `REFRESH_EVENT_SOURCE_NAME`, `_on_refresh_callbacks`, and the `add_on_refresh_callback` / `remove_on_refresh_callback` / `fire_on_refresh` APIs from `MngrCliBackendResolver`.
- Remove the `_handle_refresh_event_callback`, `_dispatch_refresh_broadcast`, `_parse_refresh_service_name`, and `_log_refresh_dispatch_result` helpers from `desktop_client.app`, along with the `_refresh_event_apps` registry and its callback registration.
- Stop dispatching the per-agent `refresh` event source in the `forward_cli` envelope consumer.
- Remove the now-dead refresh integration tests in `desktop_client.test_desktop_client` and the `forward_cli_test` envelope dispatch test for refresh.

The refresh-via-desktop-client mechanism has been superseded by an `open_tab` WebSocket broadcast from the workspace server, so the desktop-client-mediated refresh path is no longer wired up.

Workspace web content can now open a permission request from inside the workspace by posting a `minds:open-request-modal` message (with a request id) to `window.parent`. In the desktop app the content view gains a minimal, allowlist-only relay preload (no `window.minds` bridge) that forwards just this message to the main process, which validates the id and opens the same modal overlay the requests-panel card click uses. In browser mode the shell navigates the content iframe to the request page instead, since there is no overlay. Messages are only honoured from the workspace content frame and only for well-formed request ids.

Fixed a bug where re-opening a permission request that had already been approved or denied still showed the actionable grant/deny form (letting it be resolved a second time). The request page now shows a "This permission request is no longer available" notice for already-resolved or missing requests, and the grant/deny endpoints reject a repeat action on a resolved request with a 409 instead of re-applying it.

`minds env deploy` now pushes an `ovh-<tier>` Modal secret (from Vault `secrets/minds/<tier>/ovh`) alongside the other per-env connector secrets. The remote_service_connector needs OVH AK/AS/CK at runtime so its release route and hourly cleanup cron can strip per-lease tags and cancel released pool VPSes directly.

Fixed several bugs in `minds env deploy` / `recover` and the workspace-create
flow, surfaced while standing up a fresh dev environment:

- Deploy now pushes the `ovh` per-env Modal Secret. The remote-service-connector
  app references `ovh-<tier>-<deploy_id>` via `Secret.from_name` (its release
  route + cleanup cron sign OVH API calls at runtime), but the `ovh` entry was
  missing from every tier's `deploy.toml` `[secrets].services` list, so
  `modal deploy rsc-<tier>` failed with "Secret ... not found in environment".
  Added `ovh` to the dev/staging/production/ci lists and added a regression test
  asserting each tier's `secrets.services` matches `per_env_secret_services()`.
- `minds env recover` now runs non-interactively. The Modal app-stop step ran
  `modal app stop` without `-y`, which aborts with "no interactive terminal
  detected" whenever recover runs without a TTY (auto-rollback after a failed
  deploy, CI, background runs). Added `-y`.
- `minds env recover` is now re-runnable. The Neon instant-restore step was not
  idempotent: a recover that failed a later step left the pre-restore preserve
  branch behind, so re-running returned 409 ("branch with that name already
  exists") and could never delete its recover-target file. The restore now
  treats that 409 as "already restored" and proceeds.
- `minds env deploy` now exits non-zero when a failed deploy rolls back. The
  failure path execs into `minds env recover`, which inherits the exit code; a
  successful rollback therefore reported the *failed* deploy as success (exit 0),
  masking it from callers / CI. `recover` gained a hidden `--from-failed-deploy`
  flag (passed only by that auto-rollback exec) that forces a non-zero exit even
  when the rollback itself succeeds.
- `minds env activate` no longer dead-locks the recover flow. The blanket
  "refuse activation while ANY recover-target file exists" guard created a
  catch-22: `minds env recover` requires an activated env, but activation was
  blocked by the failed env's own recover-target -- so you could never activate
  the env to recover it. Activation now allows activating an env that has its
  own pending recover-target (surfacing any *other* envs' targets as a warning),
  and only hard-refuses when the pending target(s) belong solely to other envs.
- Fixed a `ty` error / runtime breakage in workspace creation from a bad merge:
  `_MngrCreateAttemptParams` still carried a `gh_token` field (and passed it to
  `run_mngr_create`) after `GH_TOKEN` had been removed end-to-end as unused, so
  the param no longer matched `run_mngr_create`'s signature and the field was
  never supplied at the construction site. Removed the leftover `gh_token`.
- Fixed the imbue_cloud fast->slow path fallback. minds decided whether to fall
  back from `fast_mode=require` by substring-matching `"FastPathUnavailableError"`
  in `mngr create`'s output, but mngr surfaces that error as a clean
  `Error: <message>` with no class name -- so the marker never matched and the
  create failed instead of falling back to the slow (rebuild) path. minds now
  parses the structured `{"event":"error","error_class":...}` JSONL record (see
  the mngr-side change), threading `error_class` through `_CreateEventCapture` ->
  `MngrCommandError` and branching on it in `_create_imbue_cloud_with_fallback`.

Also resolved a `runtime/secrets` path collision that broke Cloudflare tunnel
sharing whenever host backups were configured:

- `runtime/secrets` is now consistently a *directory* of per-secret `*.env`
  files inside the workspace, rather than a single shared file. Host backups
  already wrote `runtime/secrets/restic.env` (forcing the directory form),
  which broke the Cloudflare tunnel runner (it read `runtime/secrets` as a
  file and crashed with `IsADirectoryError`) and the Telegram injector (it
  appended to `runtime/secrets`, which fails against a directory).
- The Cloudflare tunnel token now lives at
  `runtime/secrets/cloudflare_tunnel.env`; `inject_tunnel_token_into_agent`
  writes that file (overwrite in place, no more line-strip dance).
- Added `clear_tunnel_token_from_agent`, called from the workspace
  disassociation handler after the tunnel is deleted, so the agent's
  cloudflare-tunnel service stops `cloudflared` instead of spinning against a
  now-deleted tunnel. Previously nothing ever cleared the token.
- The Telegram bot token now lives at `runtime/secrets/telegram.env`
  (overwrite in place) so it no longer collides with the other secrets.

Dev tooling: the minds desktop client launchers now pin Node automatically.

- Added `apps/minds/scripts/select_node_version.sh`, a sourced helper that
  selects the Node version pinned in `apps/minds/.nvmrc` (via nvm) before
  launching the client, so pnpm/npm's `engine-strict` check passes regardless
  of the shell's default Node. It's a no-op when the active Node already
  matches, and errors with an actionable hint (e.g. `nvm install <version>`)
  rather than auto-installing.
- `apps/minds/scripts/propagate_changes` now sources that helper before
  restarting the desktop client (`electron_start`), so the iteration loop no
  longer fails with `ERR_PNPM_UNSUPPORTED_ENGINE` when the shell's Node has
  drifted off the pin.

`minds pool destroy` now does a full teardown: it injects the activated tier's
OVH credentials from Vault (like `minds pool create`) and forwards to the admin
command, which cancels the OVH VPS before dropping the row -- so destroying a
pool host can no longer leave a stranded, still-billing VPS. Pass
`--skip-vps-cancel` only when the VPS is already gone.

Vault reads now distinguish "secret absent" from a transient failure. Added
`VaultSecretNotFoundError` (raised when the Vault CLI exits 2 / "No value
found"); `minds env deploy`'s optional-OVH-entry fallback now catches only that,
so a transient/auth Vault error no longer gets silently turned into empty OVH
credentials (which would deploy a broken `ovh` Modal Secret on a Vault blip).

Fixed a slow-path create failure on shared tiers (staging / production). The
create form there defaults to the remote FCT URL, which minds shallow-cloned;
the imbue_cloud slow path then transfers the clone to the leased host via mngr's
git-mirror push, which git rejects for shallow history ("shallow update not
allowed") -- so any create that fell back to the slow path (no fast/adopt match)
failed outright. minds now full-clones the remote URL (a single-branch,
non-shallow clone), mirroring the local-worktree branch that already
full-cloned for the same reason.

The sharing editor now waits for Cloudflare Access to go live before showing the
URL as ready. After enabling sharing, Cloudflare can take a few seconds to
publish the Access application at the edge; until then the hostname does not
return the Access login redirect, so the link looked broken. The editor now
shows a brief "Provisioning share..." state and polls a new desktop-client
endpoint (`GET /api/sharing-readiness/{agent_id}/{service_name}?url=...`) that
probes the hostname for the Access 302. It reveals the link as soon as the edge
is live, or after a short client-side timeout with a "may take a moment to
become reachable" note. Probing happens in minds (not the connector), so the
connector request stays short and the browser drives the wait.

Dropped the `paid-accounts` service from every tier's deploy config and from the per-env deploy secret list, since paid-user tracking moved from the `PAID_ACCOUNT_SUFFIXES` Modal secret to database tables managed via the connector's admin API. The paid-list admin API key (`MINDS_PAID_ADMIN_KEY`) and cache TTL now ship in the existing `supertokens` secret. Updated the vault-setup and staging-bringup docs accordingly.

Added a per-tier `[scaledown_window]` deploy.toml block (connector / litellm_proxy seconds) threaded into each `modal deploy` so containers stay hot for a configurable idle window. Dev defaults to 600s (10 min) so its no-warm-pool apps don't cold-boot every request; staging/production and the ci/test tier omit it (Modal default), so deployment tests still tear containers down promptly.

Added a per-tier `[paid]` deploy.toml block (`domains` / `emails`) that `minds env deploy` seeds (seed-if-absent) into the connector's `paid_domains` / `paid_emails` tables right after the schema migrations. All four tiers (dev, ci, staging, production) default `domains = ["imbue.com"]` so the team can use paid features on a fresh env without manual setup. Seeding uses `INSERT ... ON CONFLICT DO NOTHING`, so a redeploy never re-activates an entry an operator soft-removed.

imbue_cloud workspace creation now falls back automatically. Minds runs
`mngr create` with `fast_mode=require` first (adopt a matching pre-baked pool
host); if no exact match is available the provider raises
`FastPathUnavailableError`, and minds retries the same create with
`fast_mode=prevent`, which leases any available pool host and rebuilds it from
the FCT Dockerfile. The user-facing creation log states which path was taken.

## 2026-06-02

Add a styleguide page at `/_dev/styleguide` showing the design tokens and a small catalog of UI patterns (titlebar, sidebar items, accent spine, focus ring, shadow seam, spinner, buttons, notices, hue picker). Visible in every tier including production. Pattern demos match the actual Tailwind classes the chrome / sidebar / inputs use.

Cleaned up `static/tokens.css` to remove tokens that nothing in the app actually consumes: `--bg-chrome*`, `--border-chrome`, `--text-chrome*`, `--link`, `--focus-ring`. Only `--shadow-seam` remains in `:root` (chrome uses raw Tailwind classes for the rest). `--workspace-accent` is unaffected (it's the per-workspace inline-style hue consumed by the `.page-workspace` / `.accent-spine` / `.sidebar-item` / `.accent-swatch` rules).

Added a drift-guard ratchet: token swatches in the styleguide carry `data-token="--<name>"`, and `templates_test.py` asserts that set equals the `:root` declarations in `tokens.css`. Adding a token without a swatch (or removing a token without removing the swatch) now fails the test.

- External links clicked anywhere in the desktop app (agent content, sidebar, request panels, or the title bar) now open in the user's default browser instead of taking over the in-app workspace view or spawning a bare app window. This covers both ordinary link clicks and `target="_blank"` / `window.open` popups. In-app navigation (the app's own pages and `agent-<id>.localhost` workspace pages) is unchanged.
- When the OS has no app registered to handle a link (most commonly a `mailto:` or `tel:` link with no mail client or dialer configured), the failed open no longer silently does nothing: the app now shows a notification and copies the link (or, for `mailto:`/`tel:`, the bare email address / phone number) to the clipboard so the click is recoverable.
- Malformed external links (e.g. an `https://` URL with a stray space or parenthesis baked into it, as agents sometimes produce) are now also sent to the browser instead of opening a blank, chrome-less in-app window that fails to load.

Tiered system-interface restart for the minds recovery flow.

- When a workspace's system interface stops responding, minds shows a
  recovery page. While it is checking host health or a restart is in
  flight it shows a single "Loading workspace" state and refreshes itself
  until the workspace is back.
- The recovery page picks its tier from the workspace host's state and
  recovers with no clicks where it safely can. A running container gets a
  surgical system-interface restart (which does not interrupt your
  agents); a fully stopped container gets a full restart immediately
  (nothing is running, so there is nothing to interrupt). Only an
  ambiguous host state falls back to a confirmed "Restart workspace"
  button.
- The recovery page's pre-restart prompt and its post-failure state are
  now one identical "Workspace unresponsive" page: same heading, same
  body, a "Restart workspace" button, and a collapsed error detail that
  appears only when a restart actually failed (expandable, and it wraps
  instead of overflowing its container). The post-failure state no
  longer says "Restart failed" -- the automatic restart runs invisibly
  behind the "Loading workspace" state, so naming a failed attempt the
  user never saw was just confusing.
- The surgical restart cleanly stops and starts the system-services
  agent instead of poking its tmux window; the full restart bounces the
  whole workspace container.
- The recovery page's loading state is visually consistent with the
  forwarding plugin's "Loading workspace" loader, so the two pages a user
  may see during recovery look like one page.
- The sidebar workspace context menu gains a "Restart workspace…" entry
  (with a confirmation, since it interrupts every agent), and the home
  page gains a per-workspace restart button.
- Opening a workspace whose container has been stopped now routes to the
  recovery page (and serves the styled "Loading workspace" loader)
  instead of flashing a raw error.
- The recovery page's "Loading workspace" state no longer shows the
  explanatory "This page will reload automatically..." line -- it just
  shows the heading.
- The recovery page now auto-refreshes on a 1s cadence rather than
  1.5s, so its self-reload coincides with a completed rotation of the
  loading spinner instead of jumping the spinner back mid-rotation.
- The recovery page no longer flashes up for a workspace that is actually
  healthy. A workspace is now only treated as stuck after the background
  probe loop confirms it unreachable with a sustained run of failed HTTP
  probes; a single transient backend hiccup (such as a recycled SSE
  stream) merely starts active probing instead of triggering recovery.
- The forwarding plugin now reports every non-2xx backend response (it no
  longer pre-filters to specific status codes), so minds decides which
  ones matter: only connection-level failures and infrastructure 5xx
  (502/503/504) enroll an agent for active probing. Application errors
  (app 500s, ordinary 4xx) are ignored on the failure-envelope path and
  left for the background probe to adjudicate.
- Minds' HTTP calls through the forwarding plugin -- the
  workspace-readiness / health probes and the refresh-service broadcast
  POST -- now connect to the plugin over loopback and carry the agent's
  ``agent-<hex>.localhost`` vhost in the ``Host`` header, instead of
  putting the subdomain in the request URL. The plugin already routes on
  the ``Host`` header, so this makes those calls independent of
  ``*.localhost`` name resolution, which is not available on every host.
- Recovery diagnostics: the recovery page now runs a batched in-container
  probe (``tmux ls``, ``services.toml`` declaration parse, ``ss``/``curl``
  on the system-interface inner port) plus a plugin resolver-snapshot
  read, and surfaces the results inline. A collapsed Diagnostics
  ``<details>`` block carries the raw observations (host / SSH /
  services-agent state / services.toml / in-container probe / plugin
  resolver) and copyable SSH connection strings for the workspace host,
  with a page-level "Copy diagnostics" button. Probes only run on
  recovery-page load (RESTARTING refreshes skip probing); normal healthy
  operation generates no new probe traffic.
- New "Workspace misconfigured" recovery tier: when ``services.toml`` is
  missing ``[services.system_interface]`` (the only condition where no
  restart can possibly help), the recovery page renders dedicated copy
  explaining that a restart will not help and offers a secondary "Try
  restart anyway" affordance rather than auto-dispatching.
- Auto-escalate to host-restart when the SSH transport to a RUNNING host
  is down (the probe sentinel never returns). The page renders the
  shared "Workspace unresponsive" state, and the primary button is
  rebound to the host restart; bouncing a live container still requires
  explicit consent, so no auto-dispatch.
- The recovery probe runs over ``mngr exec`` with a 5s hard ceiling
  bounded by ``--no-start`` and ``--quiet``, so a wedged container
  cannot gate the recovery UI and a probe will never accidentally start
  a stopped host.
- On every non-HEALTHY -> HEALTHY tracker transition, the system
  interface health tracker now fires an on-recovery callback. Minds
  wires it to a loguru INFO line so the final recovery is visible in
  the log alongside the per-probe diagnostics line.
- Fix a race during sidebar-initiated workspace restarts where the
  recovery page would briefly redirect back to the workspace, then
  flip back to "Loading workspace" once the container actually went
  down. The background health probe loop now skips RESTARTING agents
  -- only the restart worker (which probes after its ``mngr stop``
  completes) can transition an in-flight restart to HEALTHY, so a
  probe of the still-alive pre-restart system interface can no longer
  prematurely declare recovery.
- Recovery-page diagnostics now show the raw ``mngr list`` invocation
  that fed every host-state field. The host-health endpoint surfaces:
  - The exact shell-quoted command (``mngr_list_command``), the raw
    ``stdout`` / ``stderr``, and the subprocess ``exit_code``. The
    diagnostics menu renders them verbatim, so the user can read the
    listing directly (which agents, which host states, which
    per-provider errors) instead of relying on minds' summarization,
    and can paste the command into a terminal to re-run it outside
    minds.
  - ``mngr_list_error``: a one-line summary of why ``mngr list`` did
    not exit cleanly -- whether the subprocess errored, the payload's
    per-provider ``errors`` array was non-empty, or the listing timed
    out. When set, the diagnostics menu surfaces it so the user can
    tell that the issue lives in a sibling workspace's host rather
    than their own.
  - ``plugin_resolver_has_services``: a self-describing boolean
    derived from the existing ``plugin_resolver_services`` map, named
    for what it means rather than asking the reader to compute it.
- The host-state ``mngr list`` is now scoped to this workspace's chat
  agent + system-services agent via a CEL ``id == ...`` include, and
  runs with ``--on-error continue`` so per-provider errors do not blank
  out the entire diagnostic. The recovery page therefore renders
  meaningful per-workspace data even when an unrelated host on the same
  provider is wedged.
- Quieter recovery-probe logs. The on-recovery INFO line now carries a
  compact summary of the cached probe (host state, ssh_dead,
  is_misconfigured, services-agent lifecycle, plugin discovery, probe
  inner port + curl status) instead of dumping the full
  ``HostHealthResponse`` JSON -- the JSON dump otherwise carried
  multi-KB ``mngr_list_*`` and ``probe.raw_stdout`` payloads with no
  programmatic consumer. The recovery probe's ``mngr exec`` subprocess
  also no longer emits a per-failure WARNING with its long
  base64-encoded inner script in the argv: probe failures (e.g. SSH
  transport down on a stopped host) are an expected diagnostic outcome
  already captured by the Layer-2 host-state INFO line via
  ``ssh_dead=True``. Restart-step and ``mngr list`` failures still emit
  the WARNING as before.
- A transient discovery loss (e.g. SSH dying inside a docker container)
  no longer kicks the user out of an open workspace window to the
  landing page. Electron now only navigates the content view to landing
  when the workspace was explicitly destroyed -- the chrome SSE
  ``workspaces`` payload includes a ``destroying_agent_ids`` list, and
  the desktop client remembers which agent ids it has ever seen
  destroying. When a workspace disappears from the live workspaces list,
  Electron checks that set; if the id is not there, the existing
  recovery flow handles the unresponsive workspace via the
  ``system_interface_status`` SSE event, with no nav.
- Minds now records the last-good per-host agent topology to a persistent
  ``last_good_agent_topology.json`` under the data directory, updated
  whenever discovery completely enumerates a host (its system-services
  agent is present). ``get_system_services_agent_id`` runs the same
  host-and-name search over the live snapshot first and falls back to this
  topology when live discovery has lost the host (the SSH-dead failure
  mode), so a restart can still address the system-services agent for
  ``mngr stop`` / ``mngr start``. Without this, a restart attempted while
  the docker provider could not enumerate agents would fail with "Could
  not locate the system-services agent for this workspace." A host whose
  enumeration is incomplete -- or that has dropped out of discovery
  entirely -- keeps its last complete record, so a partial or empty
  snapshot never erases a still-needed pairing (e.g. one wedged workspace
  among several healthy ones).
- Recovery diagnostics rewritten as a flat probe list. The host-health
  endpoint now returns ``probes: [{question, command, output, answer},
  ...]`` plus a derived ``dispatch_tier`` enum
  (``interface_unresponsive``/``host_offline``/``host_unresponsive``/``workspace_misconfigured``)
  instead of the
  prior natural-language fields (``reachable``, ``host_offline``,
  ``ssh_dead``, ``is_misconfigured``, ``host_state``,
  ``services_agent_state``, ``ssh_connections``, ``mngr_list_*``,
  ``plugin_resolver_*``). The recovery page renders each probe as a row
  with a check/x/? glyph and an expander showing the exact command and
  raw output, so the JSON object and the rendered view are kept simple
  and consistent. The page's restart-tier dispatch is now a single
  switch over ``dispatch_tier``. The cached probe-on-recovery INFO log
  and its ``_HostHealthCache`` holder were dropped along the way.
- The recovery page's "Loading workspace" state now hides the
  Diagnostics dropdown and clears the cached host-health payload, so a
  stale diagnostic from the previous tick does not linger on the page
  while a fresh check is in flight (the previous behavior was to leave
  the diagnostic visible after clicking "Restart workspace", which made
  the dropdown look like fresh data when it was already stale).
- The recovery page's restart-failed state now shows the failure error
  details and the diagnostics list together (in separate elements),
  instead of replacing the diagnostics with just the error. The page
  re-runs the host-health probe (with auto-dispatch off so it does not
  stack another restart attempt) so the user can see both the failure
  reason and the current probe answers at once.
- The post-restart startup-wait budget is now tier-aware. A surgical
  (in-place) restart still waits 15s, but a host restart -- which
  cold-boots the whole container -- now waits 30s before declaring the
  attempt failed. The previous shared 15s budget routinely bounced a
  still-booting workspace to the "Workspace unresponsive" page even
  though the container came up healthy moments later.
- A failed restart is no longer a dead end. The "Workspace unresponsive"
  page (restart-failed state) now polls in the background and, the moment
  the workspace's system interface answers again (the background health
  probe recovers it on its own -- e.g. a cold boot that finished just
  after the restart worker's wait elapsed), returns the user to the
  workspace automatically. Previously the page sat unresponsive until the
  user manually navigated away and back. The poll uses a lightweight
  redirect check, so the displayed failure reason and diagnostics stay
  put and the heavy host-health probe is not re-run on each tick.
- The auto-dispatched host restart (chosen only when the container is
  already fully stopped) now skips the redundant ``mngr stop --stop-host``
  step and cold-boots straight away, shaving a full ``mngr`` invocation
  off the recovery path. The manual "Restart workspace" button and the
  SSH-dead escalation still stop first, since they may target a
  still-running container.
- The "Is anything listening on the system-interface inner port?"
  diagnostic no longer depends on ``ss``. The agent container image ships
  no ``iproute2``, so the previous ``ss -ltnp`` probe always failed with a
  bare ``FileNotFoundError(2, 'No such file or directory')`` -- which read
  like the port was down when really the tool was simply absent. The probe
  now scans ``/proc/net/tcp{,6}`` in pure Python for a TCP_LISTEN socket on
  the inner port (decoding the listen address to ``ip:port``), so it works
  on the stock image and answers the question accurately.
- Every recovery-diagnostic row now shows a complete, copy-pasteable command
  whose stdout is exactly the output rendered beside it -- previously the
  command was the data-fetch call while the output was a value minds derived
  from it (e.g. command ``mngr list ... --format json`` but output
  ``RUNNING``), so the two did not correspond. Now:
  - The container-running and services-agent-registered rows pipe ``mngr
    list`` through ``jq -r`` to print exactly the extracted ``.host.state`` /
    ``.state`` (with a ``no host row`` / ``no agent row`` fallback line when
    the row is absent). The synthetic ``state=`` prefix is gone.
  - The in-container checks (services.toml declaration, inner-port LISTEN
    scan, local curl) are wrapped as ``mngr exec <services-agent-id>
    '<check>' --no-start --quiet`` so an operator can run them from the same
    place ``mngr`` lives, without opening a shell inside the container. Each
    inner check prints exactly the row's output: ``declared``/``MISSING`` for
    services.toml, decoded ``LISTEN ip:port`` lines (or ``(no LISTEN socket on
    port N)``) for the port scan, and the bare HTTP status code for curl.
  - The "can we run a command inside" row shows the real batched ``mngr
    exec`` and renders its verbatim stdout (the sentinel followed by the JSON
    payload).
  - The plugin-resolver row is the lone exception: its datum lives in minds'
    own memory (fed by the forward-plugin event stream) and has no in-container
    reproduction, so it stays a clearly-labelled internal observation.
- The workspace-readiness / health probes hit `/` and treat any 200 as
  "ready", deliberately decoupled from whatever application happens to be
  running inside the workspace. The probe makes no assumption about which
  app answers on the inner port or which routes it implements -- it only
  confirms that some web server is up and serving 200s for `GET /`.
- The recovery-page diagnostic that curls the inner web server inside the
  container targets `/`, for the same reason: it confirms a web server is
  answering on the inner port without coupling to any app-specific route.
  The diagnostic row reads "Does the inner web server answer GET / inside
  the container?" and its copy-pasteable `curl` command reflects the `/`
  path.
- The "Workspace unresponsive" page was restyled for a clearer hierarchy.
  The "Restart workspace" button is now the page's focal point -- a
  full-width primary button directly under the message -- rather than being
  sandwiched between the error and diagnostics dropdowns. The error and
  diagnostics disclosures are grouped together below the button under a
  muted "Troubleshooting" label, restyled from the heavy amber-filled boxes
  into quiet white cards with faint borders, a subtle shadow, and a chevron
  affordance (including on each diagnostic-question row). The troubleshooting
  block hides itself entirely whenever neither disclosure is showing, so the
  divider and label never appear over an empty section. Most users only ever
  need the button; the dropdowns are now visibly secondary, for the rare
  deep-debugging case.
- The Diagnostics menu regains a "Copy SSH command" button beside "Copy
  diagnostics". It copies a ready-to-run ``ssh -i <key> -p <port>
  <user>@<host>`` for the workspace host -- the same command mngr emits for
  the host. The per-host SSH command was previously surfaced in the
  diagnostics block but was dropped when the host-health response was
  narrowed to the flat probe list. It is now rendered server-side from the
  backend resolver's SSH info, so the host-health response stays narrow. The
  button is shown for every workspace (Docker, Lima, and remote hosts are all
  reached over SSH) and omitted only in the brief window before discovery has
  surfaced the host's SSH info.
- When the recovery page's ``mngr list`` host-state lookup does not exit
  cleanly (e.g. it times out, or a provider is unreachable) and so returns no
  row for this workspace, the "container running" and "system-services agent
  registered" diagnostic rows now show the failure reason (``mngr list
  failed: ...``) in place of a bare "no row", so the user can tell the
  listing failed rather than concluding the host or agent is genuinely
  absent. When the listing still returns this workspace's own row despite a
  non-clean exit, the real row is shown as before.
- The "Workspace unresponsive" recovery page no longer pushes its heading and
  "Restart workspace" button off-screen when several Troubleshooting
  disclosures are expanded. The card is now capped to the viewport height and
  laid out as a vertical stack: the heading and the restart button stay pinned
  at the top, and only the troubleshooting block (error details + diagnostics)
  scrolls internally once its content overflows. Previously the whole card grew
  past the viewport and, because it is vertically centered, the heading and
  button slid above the top edge out of reach of the page scrollbar.
- Fix: a misconfigured workspace (``services.toml`` missing
  ``[services.system_interface]``) now renders the "Workspace misconfigured"
  page even after a failed restart. Previously the misconfigured tier was only
  honored on the live stuck/probe entry path; on the ``restart_failed`` entry
  path -- which is exactly where a misconfigured workspace ends up once its
  undeclared interface fails to come back up -- the recovery page
  short-circuited to the generic "Workspace unresponsive" state before
  inspecting the dispatch tier, so the diagnostic correctly flagged the missing
  block while the page still implied a restart could help. The
  ``workspace_misconfigured`` check now runs ahead of the no-auto-dispatch
  short-circuit, so this tier is honored on every entry path.
- Internal: the ``mngr`` subprocess helper that drives the restart steps and
  the host-health probe returns stdout on a clean exit and raises a single
  ``MngrCommandError`` for any non-clean outcome (timeout, nonzero exit, or
  failure to launch), matching how the rest of minds shells out to ``mngr``
  (``run_mngr_create``, the destroy cleanup). A restart step marks the workspace
  "Restart failed" with the reason; the host-health probe threads it into its
  response.
- The host-health ``mngr list`` probe scopes discovery to the workspace's own
  provider via ``--provider``, so an unrelated provider being unreachable cannot
  blank out this workspace's host state. If a sibling host on the same provider
  fails discovery while this workspace needs recovery, the recovery page falls
  back to a manual "Restart workspace" click instead of auto-dispatching.

The typed `GET /permissions/available` catalog entry (`AvailableServiceEntry`) now carries detent's `$comment` summaries: a scope-level `description`, and a `permissions` list whose elements are `AvailablePermission` objects (`name` plus an optional `description`) instead of bare strings. Both descriptions are optional/default-empty so older catalogs still validate.

The predefined permission request dialog now reads those descriptions through the services catalog (`ServicePermissionInfo` gained a scope `description` and a `description_by_permission_name` map) and shows each permission's summary, when present, beside its name (at the same font size as the name). The default view renders the to-be-granted permissions as a checkmark-led list. The scope-level summary is not surfaced on the dialog.

Fixed the desktop app's live permission-request notifications, which previously never updated: the requests badge, the requests-panel auto-open, and the in-panel list only refreshed after the user manually closed and reopened the panel.

Two root causes:

- The chrome SSE stream keyed its change detection off the bare pending-request *count*. Because latchkey requests are deduplicated by `(agent_id, scope, request_type)`, re-requesting the same scope (or resolving one request while another arrives) keeps the count constant while the contents change, so no update was emitted. The stream now diffs a content-based payload (`count` plus the ordered list of pending `request_ids`) and emits whenever the pending *set* changes. The SSE event was renamed from `request_count` to `requests` to reflect that it carries the id list, not just a count.

- The Electron main-process SSE consumer (`runChromeSSELoop`) wedged permanently the first time the auth-cookie sync forced a reconnect: `req.abort()` does not emit a terminal event on Electron's `ClientRequest`, so the awaited connection promise never resolved and the live consumer died seconds after launch. The loop now resolves that promise directly on a forced reconnect (via a shared finish ref) instead of relying on `'abort'`/`'close'` events, the latter of which fired eagerly on healthy streaming responses and caused a reconnect storm that leaked backend SSE generators and exhausted the connection pool.

In the Electron consumer, the requests panel now refreshes whenever the pending id set changes (not only on a count increase), and auto-open triggers when a genuinely new request id appears (so approving/denying never reopens a panel the user closed).

Permission request dialogs now open in a modal overlay instead of replacing the main content window.

When a user clicks a permission request card in the side panel, the request page (`/requests/<event_id>`) now opens in a transparent full-content-area overlay (`modalView`) stacked above the workspace, with a dim backdrop. The workspace view is never navigated away, so the user keeps the context of their work; dismissing the dialog (via Approve/Deny, the close button, a backdrop click, or Escape) returns them to exactly where they were. Opened directly in a browser with no modal host, the page degrades to a dimmed, centered card and dismissal navigates home.

Permission requests are no longer collapsed by service/scope/path. The request inbox now keys pending requests solely by request ID, so every distinct request the agent makes shows as its own card. Previously, multiple requests sharing the same agent, scope, and permissions were merged into one, making Approve/Deny appear to do nothing (a hidden duplicate would surface in place of the resolved one). Redeliveries of the same request (same request ID) are still collapsed so the panel does not duplicate cards.

Reworked the workspace creation flow into a guided onboarding experience.

- The Create Workspace form is now name-first: just a workspace name and a Create button up front, with a "Configure..." disclosure for the compute / AI / backup providers and a nested "Show advanced settings" disclosure for the repository, branch, and GH_TOKEN. The account selector moved to a compact menu at the top right.
- After clicking Create, the workspace is created in the background while the user answers three short onboarding questions. If creation finishes before they're done, they go straight into the workspace; otherwise they see a styled loading screen with a progress bar, rotating tips, and a "Show details" toggle over the live creation log.
- The three questions wire up minimal behavior (each is optional):
  - "Is it OK if I get to know you?" runs a small local scan of your machine (your name) and saves it to `~/.minds/user_context/<creation-id>.json` unless you choose full control.
  - "What should we start with?" sends your description to the workspace's chat agent once it comes online.
  - "How do you want to deal with permissions?" is written into the workspace's Claude memory at `runtime/memory/permissions_preferences.md`.
- `POST /api/create-agent` now accepts optional `user_data_preference`, `initial_problem`, and `permissions_preference` fields; omitting them preserves the previous behavior. A new `POST /api/create-agent/{id}/onboarding` endpoint backs the form flow.

`apps/minds/scripts/build_test.py` no longer silently skips in CI. PR #1772 renamed `apps/minds/todesktop.json` to `apps/minds/todesktop.js`, so `test_bundled_limactl_is_signed_with_virtualization_entitlement` evaluates the config via `node` and carried `skipif(node is None)` -- which skipped silently on the Node-less offload image. Now that Node is installed in the shared mngr image, the `skipif` is removed: the test runs on offload and asserts Node is present, so a missing Node is a hard failure rather than a silent skip.

## 2026-06-01

The latchkey services catalog now maps each raw service name to a list of scope entries instead of a single entry, so one service can expose more than one detent scope. `LatchkeyGatewayClient.get_available_services` now returns `dict[str, tuple[AvailableServiceEntry, ...]]`, and `ServicesCatalog.get` / `ServicesCatalog.as_mapping` now return a tuple of `ServicePermissionInfo` per service. Per-scope lookup via `ServicesCatalog.get_by_scope` is unchanged.

## 2026-05-29

Exclude the Latchkey dependency from the minimum age check (we are co-developing Latchkey together with Minds).

Simplified the latchkey predefined-permission approval dialog for non-technical users. By default it now shows a read-only, informative list of the permissions the agent is requesting (no checkboxes), with only Approve and Deny actions. A small "Adjust" link (rendered inside the permission list, aligned with the permission names) reveals the full per-permission checkbox editor (the previous appearance) for users who want fine-grained control. The dialog was also visually streamlined: the standalone "Workspace:" line was removed, the agent's reason is now attributed prominently as "<workspace> says:", the request summary is a single sentence ("Approving will grant <workspace> and its sibling agents the following permissions:"), and the service name in the header no longer renders inside a grey box. The file-sharing permission dialog was updated to match this chrome (same header treatment, "<workspace> says:" attribution, dropped workspace line, and a single summary sentence naming the workspace, access level, and host-wide scope). The rationale text is italicized, the "Adjust" link is right-aligned within the permission list and separated from it by a faint divider, and the page now reserves the scrollbar gutter so expanding the editor no longer shifts the layout sideways.

- `apps/minds`: activate the ToDesktop `beforeInstall` hook so the build
  server re-downloads/re-resolves `uv` and `git` for its target platform
  rather than using the bytes uploaded from the developer's machine.
  Wires `package.json`'s `todesktop:beforeInstall` to
  `./scripts/download-binaries.js`, and restores the `downloadUv()`
  orchestrator in that file (it had been removed in the bundled-git
  carve-out because it was dormant without this PR's hook wiring).
- `apps/minds`: pin both `pnpm` and `node` via ToDesktop's first-class
  `pnpmVersion` / `nodeVersion` config fields, sourcing the literal
  values from `package.json`'s `engines` block (which #1710 already
  pins to `pnpm 10.33.4` and `node 24.15.0`). To make this work,
  `todesktop.json` is replaced with a `todesktop.js` that does
  `require('./package.json')` and reads `engines.pnpm` and
  `engines.node` into the `pnpmVersion` and `nodeVersion` ToDesktop
  config fields; ToDesktop's CLI supports `.json`, `.js`, and `.ts`
  config formats. Net effect: `package.json` is now the single source
  of truth for the pnpm + node versions used on dev laptops (via
  `engines` + `.nvmrc`), in imbue CI (via the workflow's explicit
  installs, still a separate pin), and on ToDesktop's runner (via
  `todesktop.js` reading `package.json`). Replaces a draft of this
  PR that had a home-rolled `installPnpm()` fallback ladder
  (~80 LoC + a 14-line rationale comment) -- ToDesktop's runtime
  already provisions the requested versions before installing
  dependencies, so the ladder was working around the absence of a
  knob that isn't absent. Empirically verified end-to-end against a
  draft ToDesktop build from `wz/minds_onboard` (build
  `260528yf2ma2jd4`) with the earlier `"pnpmVersion": "10.33.4"`
  spelling: both Linux and Mac arm64 finished, packaged binary
  launches and round-trips a first message E2E. The `beforeInstall`
  hook stays for `uv` + `git` (no first-class ToDesktop knob).
  `apps/minds/scripts/build_test.py` (which reads the ToDesktop config
  to assert the limactl signing contract) now shells out to `node -e
  "console.log(JSON.stringify(require('./todesktop.js')))"`. It
  module-level-skips via `pytest.mark.skipif(shutil.which('node') is
  None, ...)` when no node is on PATH -- matches the existing
  `mngr_latchkey` precedent for Node-dependent Python tests. Coverage
  gap: this test currently doesn't run in the offload sandbox (no
  node there). Adding node to the offload image -- or to a
  minds-specific sandbox image -- is a follow-up.
- `apps/minds`: consolidate `downloadUv` into a single definition in
  `scripts/download-binaries.js` and import it into `scripts/build.js`,
  mirroring how `downloadGit` and `download` are already shared.
  Removes the duplicated `UV_VERSION` constant, `getUvDownloadUrl`,
  and `downloadUv` from `build.js`. Both call sites (local
  `pnpm build` and ToDesktop's `beforeInstall` hook) now run the same
  implementation against their own resources directory.

- Resetting or destroying an env no longer leaves its mngr Docker state container (`<MNGR_PREFIX>docker-state-<user_id>`) running forever. Both `minds env destroy` and the activate-time generation-mismatch auto-wipe now remove that env's exact state container and its backing volume.
- The auto-wipe now also destroys the env's mngr agents (in a single `mngr destroy` call) before wiping local state, so their Docker host containers and build images are cleaned up too.
- Env-teardown agent destruction now uses one batched `mngr destroy` call instead of one call per agent.

The "destroy workspace" UI action now releases the underlying
imbue_cloud-leased host's lease immediately rather than waiting the 7-day
destroyed-host grace period for mngr's GC to run `delete_host`. The
implementation lives in `mngr destroy` (see `libs/mngr/changelog/`);
minds' destroy command was previously *intentionally* not chaining lease
release because the grace-period delegation was the design. That
intentional decision is no longer correct -- `mngr destroy` now drops
cloud-side resources up front, and the grace period only retains
historical state. The stale "Lease release is intentionally NOT chained
here" comment in `destroying.py` is updated to reflect the new contract.

# Self-hosted Mac runner support

- Added `apps/minds/scripts/mac-runner-reset.sh`: cleans `~/.minds`, removes the installed `.app`, kills leftover Minds processes, and stops/deletes any Lima VM instances. Optionally re-downloads + installs a fresh `.app` from a ToDesktop `.zip` URL passed as the first argument. Intended to run at the start of every verification job on the dedicated self-hosted mac-runner so each run starts from a known-clean state. Preserves only the Lima base-image cache (`~/Library/Caches/lima/`), which is ~1.5 GB and unrelated to Minds itself.

Added a "Backup provider" control to the workspace create form, mirroring the
existing "AI provider" toggle, with three options:

- `imbue_cloud` -- creates a per-workspace R2 bucket (named after the new host
  id) and a scoped key, then injects a `runtime/secrets/restic.env` pointing
  the FCT `host_backup` service at that bucket. Gated on a selected account;
  the default when an account is present.
- `manual` -- a free-form `KEY=VALUE` block written verbatim to `restic.env`
  (you supply `RESTIC_REPOSITORY` and backend credentials).
- `configure_later` -- injects nothing now; the default when no account is
  selected.

When a real backup provider is chosen, a "Backup encryption method" row
appears: `master_password` or `no_password`. The conditional backup fields
(restic environment, encryption method, master password) render as standard
label-on-left / field-on-right rows like the rest of the form.

minds (which now requires `restic` to be installed on the machine running it)
initializes each workspace's restic repository itself and gives the workspace
its own random repository password, so the master password never enters the
workspace. Enabling backups: resolve the repository + credentials, generate a
random per-workspace password, `restic init` the repo with the master
password (or empty for `no_password`), `restic key add` the random password,
write the canonical `restic.env` to a 0600 minds-side file, and inject that
file into the workspace. The `manual` block must not set `RESTIC_PASSWORD`
(minds assigns it).

A freshly-minted imbue_cloud (Cloudflare R2) credential takes a few seconds to
become active at the storage backend's edge, so the immediate `restic init`
could fail with a transient `Unauthorized`. minds now retries the `restic init`
/ `restic key add` bootstrap on such transient auth failures for a bounded
window, so backup provisioning rides out that propagation delay instead of
failing outright.

Backup setup runs asynchronously after the host is created (mirroring the
Cloudflare tunnel-token injection) and is non-fatal: a failure surfaces as a
notification and leaves the workspace running. The reusable
`configure_backups_for_host` operation can be re-applied to an existing host
later and is idempotent (an existing canonical env is re-injected; an
already-created bucket / initialized repo is reused). The canonical
`restic.env` is never auto-deleted, so a stopped or destroyed workspace's
backups stay recoverable.

The Projects page now shows each project's backup status (Backing up / Backed
up N ago / No backups / Unknown), fetched once on load from a new
`/api/backup-status` route that queries restic per project from the minds
machine. While that request is in flight each tile shows "Checking backups…",
and a freshly-created workspace with no backups yet shows "Created N ago"
(for the first 75 minutes after creation) instead of "No backups", so a brand
new project doesn't look alarming before its first backup has had a chance to
run. The route includes each workspace's creation time for this.

A backed-up project also gets a "download" link next to its status that exports
the latest snapshot as a zip. minds builds the zip on demand from the minds
machine (no workspace access needed) by restoring the latest snapshot to a temp
dir and zipping it -- `restic restore` downloads in parallel and is ~50x faster
than `restic dump --archive zip` (which fetches blobs sequentially: ~5 min vs
~10 s for a ~95 MiB snapshot). The zip lands in a /tmp file keyed by host id
(so re-exports overwrite rather than accumulate) and is served via a new
`GET /api/backup-export/{agent_id}` route; the temp restore dir is always
cleaned up. The link shows a spinner and "exporting…" while the zip is built.

New `BackupProvider` / `BackupEncryptionMethod` primitives; new
`mngr imbue_cloud bucket ...` wrappers on the imbue_cloud CLI client.

## 2026-05-28

Pin minds desktop client JS toolchain to exact versions: pnpm 10.33.4 and Node.js 24.15.0. With `engine-strict=true` plus exact `engines.node`/`engines.pnpm`, installs fail fast on mismatch instead of breaking in confusing ways. Added `.nvmrc` so nvm/fnm users pick up the pinned Node automatically. Documented how existing developers can install the pinned versions (nvm/fnm for Node, `npm install --global pnpm@10.33.4` for pnpm, with a note that Homebrew's `@<major>` kegs drift) in `apps/minds/docs/desktop-app.md`. Also pinned end-user Python to `==3.12.13` in `apps/minds/electron/pyproject/pyproject.toml` (the packaged-app pyproject that uv reads at first launch) so every install downloads the same interpreter instead of the latest 3.12 patch available at the time.

Added a 7-day dependency cooldown (minimum release age) for supply-chain hardening: `minimumReleaseAge: 10080` in `apps/minds/pnpm-workspace.yaml` (pnpm) and `exclude-newer = "7 days"` under `[tool.uv]` in the packaged `apps/minds/electron/pyproject/pyproject.toml` (uv). Both refuse to resolve any distribution -- including transitive ones -- published within the last week, so a freshly-compromised release cannot be pulled in before it is noticed. The cooldown only affects resolution; frozen-lockfile installs are unaffected.

Upgraded Electron from `35.7.5` to `40.10.1` so the runtime shipped to end users bundles Node.js `24.15.0` -- matching the exact Node version pinned for development (`engines.node` / `.nvmrc`). Previously the bundled Electron shipped a different (Node 22.x) runtime than the one developers built against. Electron 40 is the lowest major on the Node 24 line, so 40.10.1 is the smallest jump that reaches the pinned `24.15.0`; staying on 40 avoids the behavior changes introduced in 41 (cookie `changed`-event cause values) and 42 (macOS notifications require code-signing) that a jump to the newest line would pull in, none of which our Electron code depends on today but which would otherwise widen the review/test surface. 40.10.1 also clears the new 7-day pnpm cooldown (published more than a week ago), so it needs no `minimumReleaseAgeExclude`.

Bumped the bundled `UV_VERSION` in `apps/minds/scripts/build.js` from `0.7.12` to `0.11.15`. uv only supports the relative-duration form of `exclude-newer` (e.g. `"7 days"`) as of 0.10.0; the older 0.7.12 fails to parse it, silently ignores the cooldown, and -- because the committed lockfile was generated with a timestamp cutoff that 0.7.12 then sees as "removed" -- discards the lockfile and re-resolves unpinned at end-user first launch. (The lockfile's `revision = 3` format is not the issue: 0.7.12 reads revision-3 lockfiles fine; the trigger is purely the unparseable relative `exclude-newer`.) Bumping to 0.11.15 makes the shipped uv able to parse the cooldown, so it takes effect and the committed lockfile is honored.

Bumped the dependency cooldown (minimum release age) for the minds desktop toolchain from 7 days to 14 days. `minimumReleaseAge` in `apps/minds/pnpm-workspace.yaml` is now `20160` minutes (pnpm), and `exclude-newer` under `[tool.uv]` in the packaged `apps/minds/electron/pyproject/pyproject.toml` is now `"14 days"` (uv). The packaged `uv.lock`'s metadata `exclude-newer-span` was bumped in parallel from `P7D` to `P14D` to stay consistent without a full re-resolve. Behavior is unchanged otherwise: the cooldown only bites during resolution, frozen-lockfile installs are unaffected.

Bump Latchkey dependency to 2.12.2. The newest Latchkey version properly shows the ToS dialog to first-time Google Cloud users.

- `apps/minds`: bundle the real macOS `git` binary plus its `libexec/git-core`
  helpers instead of the xcode-select shim. The previous inline `downloadGit()`
  in `scripts/build.js` ran `which git`, which on macOS returns the 118 KB
  `/usr/bin/git` shim -- a launcher that re-invokes the real git from
  `/Library/Developer/CommandLineTools/`. Bundling the shim into a sandboxed
  packaged app meant runtime `git clone` SIGKILLs on any Mac without Xcode CLT
  installed at the expected path. The new `scripts/download-binaries.js`
  resolves the real binary via `xcrun --find git`, copies it plus its
  `libexec/git-core` helpers and templates, and SHA256-verifies all
  downloaded archives. Also bumps the ToDesktop `uploadSizeLimit` from 300 to
  600 because the real binary plus its libexec push the bundle over the
  previous limit.

# Desktop e2e opts FCT's config into the pytest guard (test-only)

mngr's `is_allowed_in_pytest` config field now defaults to `False`, and every
config loaded during a pytest run must opt in. The desktop-client Docker e2e
(`test_desktop_client_e2e.py`) deliberately loads forever-claude-template's real
`.mngr/settings.toml` (it pins `MNGR_ROOT_NAME=mngr` to get the create
templates), so it now adds `is_allowed_in_pytest = true` to that checkout for the
duration of the test and restores it afterward. The opt-in is intentionally
added in-test (not shipped in FCT's config, which would disable the guard for
every FCT-based project). Test-only change.

# Extract Electron e2e workspace creation flow into a reusable runner

Split the Playwright-over-CDP driver out of
`apps/minds/test_desktop_client_e2e.py` into a new module at
`apps/minds/imbue/minds/desktop_client/e2e_workspace_runner.py` so the
same flow can be invoked outside pytest. The new module exposes the
public entry points `create_workspace_via_electron`, `resolve_fct_path`,
`ensure_minds_env_defaults`, `configure_logging`, `find_free_port`, and
`destroy_agent_best_effort`; everything else stays underscore-prefixed.

The existing pytest test was reduced to a thin wrapper that:

- calls `ensure_minds_env_defaults(setenv=monkeypatch.setenv)` so any
  injected env vars get reverted between tests,
- delegates the actual Electron / Playwright flow to
  `create_workspace_via_electron`, and
- always calls `destroy_agent_best_effort` in `finally` so a successful
  test never leaks an agent into the host.

`scripts/snapshot_minds_e2e_state.py` is the second caller: it invokes
`create_workspace_via_electron` directly and deliberately omits the
`mngr destroy` cleanup, because the whole point of the snapshot is to
capture a sandbox in which the workspace's Docker container is alive.

Also added a `*/desktop_client/e2e_workspace_runner.py` exclusion to the
`test_prevent_direct_subprocess` ratchet, since the new module
necessarily shells out to `electron`, `git`, and `uv run mngr destroy`
(operator-tool subprocesses with no `ConcurrencyGroup`-managed
equivalent). No user-visible behavior change.

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

Bump Latchkey dependency to 2.12.1. The newest Latchkey version is capable of reusing Google Projects which is important because the default limit on Google Project count is low.

# Fix a signing-key generation race that intermittently logged users out

`FileAuthStore.get_signing_key` generated the cookie signing key lazily
on first access without any synchronization. FastAPI dispatches sync
route handlers on a threadpool, so on a fresh data directory the desktop
client's startup burst -- `/authenticate` plus the `/` redirect target,
`/_chrome`, and `/welcome`, each of which checks authentication -- could
all reach key generation concurrently. Two interleavings both broke auth:

- A reader saw the just-created key file as momentarily empty (the old
  code did a non-atomic `write_text`) and raised `SigningKeyError`, so
  `/authenticate` returned 500 and no session cookie was set.
- Two threads each generated a *different* key and raced to write it;
  the last writer won and silently invalidated the cookie that had just
  been signed with the earlier key, so the next request's
  `verify_session_cookie` failed and the user appeared logged out.

Either way the subsequent page load came back unauthenticated. This was
the dominant cause of flaky failures in the `test-docker-electron` CI job
(`test_create_local_docker_workspace_via_electron` timing out on the
`#create-form` selector because `GET /create` returned 403).

`get_signing_key` now reads the existing key on the fast path and, when
it must generate one, serializes generation behind a per-store lock with
a double-checked re-read and writes the key via `atomic_write` so a
concurrent reader never observes a partial file. Concurrent first-time
callers now always converge on a single persisted key.

Fixed the Deny button on the latchkey permission-request dialog so it works even when the requested scope is not in the gateway's services catalog (e.g. a typo from the agent or a stale catalog). Previously clicking Deny returned `{"error": "Scope 'XYZ' is not in the gateway catalog"}`; the deny flow now falls back to the raw scope string for both the persisted response event and the agent-facing message, so the pending request is always torn down and the agent is always notified.

# ty 0.0.39 type fix

- `_resolve_ws_name_and_account` now returns `list[AccountSession]` instead of `list[object]`. `ty` 0.0.39 rejected the previous annotation because `list` is invariant (`list[AccountSession]` is not assignable to `list[object]`); the precise element type is also more accurate.

No user-facing behavior change.

## 2026-05-26

# Minds API access: gateway-only, single key, per-agent URL prefix

The minds desktop client used to expose its `/api/v1/...` REST API to
workspaces over a per-agent reverse SSH tunnel, writing the resulting
URL to `$MNGR_AGENT_STATE_DIR/minds_api_url` and injecting a per-agent
UUID4 `MINDS_API_KEY` into each new host's env file. None of that is
how agents actually reach the Minds API anymore -- the latchkey
gateway's `minds-api-proxy` extension already handled it -- so the
machinery is gone:

- `minds run` no longer asks the `mngr forward` plugin for a
  `--reverse 0:<port>` tunnel and no longer registers any
  `on_reverse_tunnel_established` callback. The `MindsApiUrlWriter`
  and `LocalAgentDiscoveryHandler` classes (and their tests) have
  been removed from `forward_cli.py`.
- `agent_creator.py` no longer generates a per-agent `MINDS_API_KEY`,
  no longer adds `--host-env MINDS_API_KEY=...` to `mngr create`, and
  no longer stores any per-agent `api_key_hash` file. Workspaces no
  longer carry the env var at all.
- The `apps/minds/imbue/minds/desktop_client/api_key_store.py` module
  has been rewritten around a single central key, freshly generated
  in memory on every `minds run` via `generate_api_key()`. The key is
  not persisted to disk -- the supervisor is always restarted on
  minds startup and gets the current value in its env, the bare-
  origin auth gate sees the same in-memory value, and no other
  process reads the key. Rotating per-startup removes a long-lived
  secret from the filesystem.
- The `/api/v1/...` bearer-auth gate (used by both `api_v1.py` and the
  WebDAV mount under `/api/v1/files`) now compares the inbound
  `Authorization: Bearer <key>` against that single value with a
  constant-time check. Routes that need an agent id take it from the
  URL path -- the auth dependency itself returns `None`.
- The notifications endpoint moved from `POST /api/v1/notifications`
  to `POST /api/v1/agents/<agent_id>/notifications`, matching the
  Telegram routes. Every `/api/v1` route is now per-agent.
- Every agent created by minds gets added to the host's
  `minds-api-proxy-allowed-agent` enum at finalize-host-permissions
  time. The baseline permissions file's first rule rejects any
  `/minds-api-proxy/api/v1/agents/<id>/...` whose `<id>` is not in
  that enum, so an agent on host A cannot reach the Minds API on
  behalf of an agent on host B (B's id only appears in B's host's
  permissions file).
- The desktop client now calls
  `imbue.mngr_latchkey.agent_setup.register_agent_for_host(...)` directly
  -- a single library call that does an atomic file edit -- instead of
  the previous gateway-extension dance that POSTed two schemas + one
  rule per agent. The `gateway_client` field on `AgentCreator` is
  gone; `LatchkeyGatewayClient` keeps its existing user-grant API
  (`set_permission_rule`, etc.) but no longer ships the low-level
  schema-altering methods (`set_permission_schema`,
  `delete_permission_schema`, `delete_permission_rule`).
- The operator-facing equivalent is the new
  `mngr latchkey register-agent --host-id ID --agent-id ID` CLI command.
- The `inject_tunnel_token_into_agent` helper moved out of
  `api_v1.py` into its own module so it can be imported without
  pulling the FastAPI router in.

Documentation:
[`apps/minds/docs/latchkey-permissions.md`](docs/latchkey-permissions.md)
now has a "Minds API access through the gateway" section describing
the new model; [`specs/minds-rest-api/spec.md`](../../specs/minds-rest-api/spec.md)
has a banner pointing out which parts are superseded.

- Renamed the minds ``LaunchMode.LOCAL`` compute provider to ``LaunchMode.DOCKER`` everywhere (Python code, ``/create`` form HTML, ``/api/create-agent`` JSON payloads, docs). The mode has always meant "Docker container on the user's machine"; the old name collided with mngr's own ``local`` provider (which runs agents as host processes), so the rename eliminates that ambiguity. The other modes (``LIMA``, ``CLOUD``, ``IMBUE_CLOUD``) are unchanged. ``/api/create-agent`` and the create form now expect ``launch_mode=DOCKER`` instead of ``LOCAL``; submitting ``LOCAL`` is no longer recognized.

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

- `apps/minds`: bundle Lima into the desktop app. `scripts/build.js` now downloads the official Lima 2.1.1 release tarball for the build host's platform/arch and extracts it into `resources/lima/`; the packaged backend prepends `resources/lima/bin` to `PATH` so `limactl` is found without a separate `brew install lima` step. The unsigned `lima-guestagent.Darwin-*.gz` Mach-O payloads are stripped after extraction -- they break macOS notarization and are unreachable (we run Linux VMs only). A new `entitlements.mac.plist` carries `com.apple.security.virtualization` (required by `limactl`'s VZ driver), and `todesktop.json` wires it in via a `mac` block that also deep-signs the bundled `limactl`. On macOS Apple Silicon this is fully self-contained via Lima's `vz` backend; macOS Intel and Linux still require QEMU on the host machine.
- `apps/minds`: bump `workspace_ready_timeout_seconds` from 60s to 300s (`agent_creator.py`). First-boot provisioning (uv sync, npm ci + run build for the system_interface frontend) regularly takes 90-180s on a fresh VM or Docker host, so the 60s default was bouncing users to the recovery page while the agent was still finishing provisioning. The probe is cheap so a generous cap is harmless.

- Fixed a stale `LaunchMode.LOCAL` reference in `agent_creator_test.py` that was missed during the `LaunchMode.LOCAL` -> `LaunchMode.DOCKER` rename, which was causing `test_no_type_errors` to fail. No user-visible behavior change.

Hardened the workspace-restart shell command in `desktop_client/app.py` to use
exact-session matching. The previous `tmux kill-window -t "${MNGR_PREFIX}system-services:svc-system_interface"`
form had no leading `=`, so if the `${MNGR_PREFIX}system-services` session was gone
but a sibling-prefix session was alive, the kill-window could silently land on the
wrong agent's session and kill a window there. The command now uses
`-t "=${MNGR_PREFIX}system-services:svc-system_interface"` so tmux refuses to misroute.

To prevent recurrences, adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule
(added in `imbue_common`) via `rc.check_bare_tmux_targets(_DIR, snapshot(0))` in
this project's `test_ratchets.py`. The ratchet flags new occurrences of
`tmux <subcmd> -t '<bare-name>'` -- targets without a leading `=` exact-match
prefix, which can silently route commands to a sibling session whose name shares
a prefix with the intended one. The adopting test starts at a baseline of zero
violations.

## 2026-05-22

`minds run` no longer dictates the `mngr forward` plugin's port. The `--mngr-forward-port` flag and the `MINDS_MNGR_FORWARD_PORT` environment variable are removed: the plugin now picks its own port (its default, or an OS-assigned fallback when the default is taken) and reports it back via its `listening` envelope. `minds run` blocks briefly at startup until that envelope arrives, then uses the reported port for everything downstream; if the plugin fails to report a port within 5s, startup aborts with a clear error.

- Bump bundled Latchkey version to 2.11.3.

Latchkey gateway ships a new bundled `minds-api-proxy` extension that
transparently reverse-proxies requests under `/minds-api-proxy`
to the minds desktop client's bare-origin "Minds API". The upstream URL
is read at request time from the `LATCHKEY_EXTENSION_MINDS_API_URL`
environment variable, and is published to the detached
`mngr latchkey forward` supervisor (via the new
`LatchkeyForwardSupervisor.extra_env`) on every `minds run` startup, so
the proxy always points at the live Minds API port even when minds
re-binds on restart. The extension responds 503 when the env var is not
configured; requests still go through the gateway's normal permission
check.

The Latchkey gateway's `permission-requests` extension grows a typed
request schema and a new approve endpoint:

* `POST /permission-requests` now takes `{agent_id, rationale, type,
  payload}` instead of the legacy flat `{scope, permissions, ...}`
  shape. The `type` field is `"predefined"` (payload
  `{scope, permissions}`) or `"file-sharing"` (payload `{path}`,
  absolute-only, no `..` segments).
* Each pending request is persisted with the additional `target`
  (the extension's per-request `permissionsConfigPath`) and `effect`
  (a precomputed `{rules?, schemas?}` patch) fields. Pending requests
  live under `<latchkey-directory>/permission_requests/v2/` -- the
  `v2` segment is the on-disk schema version so future shape changes
  can land in a fresh directory rather than trying to migrate files
  in place.
* `POST /permission-requests/approve/<request_id>` is new. It reads
  the pending request, merges its `effect.rules` (union by scope
  key) and `effect.schemas` (overwrite by name) into the stored
  `target` permissions.json (creating it if missing), and deletes
  the pending request file. Returns the fresh permissions file in
  the response body.
* The legacy `DELETE /permission-requests/<id>` continues to remove
  a pending request without applying its effect; the minds desktop
  client uses it for the deny path.
* The `file-sharing` effect now targets the WebDAV mount described
  below. The effect attaches a per-file permission to the
  pre-existing `latchkey-self` scope from the agent baseline rather
  than minting its own scope schema. The per-file permission schema
  matches the URL path via a regex `pattern` rooted at the WebDAV
  URL for the requested resource
  (`/minds-api-proxy/api/v1/files<absolute_path>`): the exact path,
  the same path with a trailing slash, and any sub-path nested
  below it. A grant on a directory therefore transitively covers
  every file and sub-directory inside it. `..` segments do not need
  to be rejected by the pattern because the gateway's permission
  check sees the WHATWG-normalised `pathname`, which has already
  collapsed both literal `..` and percent-encoded `%2e%2e` away.
  The legacy `queryParams.path` constraint is gone.
* File-sharing requests now carry a required `access` field on the
  payload (`READ` / `WRITE`). `READ` unlocks the non-mutating WebDAV
  verbs only (`GET`, `HEAD`, `OPTIONS`, `PROPFIND`); `WRITE` is a
  strict superset that also unlocks the single-path mutating verbs
  `PUT`, `DELETE`, `PROPPATCH`, `MKCOL`, `LOCK`, `UNLOCK`. `COPY` and
  `MOVE` are intentionally excluded -- both carry a second path in
  the WebDAV `Destination` header that the per-file permission schema
  cannot constrain, so granting either would let an agent write to a
  different file in the share than the one actually shared. Per-file
  permission schemas embed the access mode in their name
  (`minds-file-server-read-<hash>` / `minds-file-server-write-<hash>`)
  so the two grants are independent. The minds approval dialog shows
  a green "read-only" or amber "read & write" badge inline next to
  the requested file path and explains what the agent will be allowed
  to do; the granted / denied
  notification text reflects the mode as well.

The minds desktop client's latchkey-permission handler code was
reorganised so the two permission request types now live as siblings
under a single `imbue.minds.desktop_client.latchkey.handlers`
package: `.predefined` (the existing catalog-backed flow, moved from
`latchkey/permissions.py`) and `.file_sharing` (moved from
`latchkey/file_sharing.py`). Their shared helpers (`MngrMessageSender`
and the Jinja-template renderers) live alongside them in the same
package. The file-sharing approval dialog now uses the same Jinja
template + Tailwind base (`templates/permissions.html`) and visual
style as the predefined dialog instead of a hand-written HTML page.

The minds desktop client side learns to render and resolve both
request types:

* `LatchkeyPermissionRequestEvent` was renamed to
  `LatchkeyPredefinedPermissionRequestEvent` to mirror the wire
  `type=predefined` and to distinguish it from the new file-sharing
  event (both flow through Latchkey).
* A new `LatchkeyFileSharingPermissionRequestEvent` (and
  accompanying `FileSharingGrantHandler`) renders a single yes/no
  dialog per absolute file path. Approval calls
  `POST /permission-requests/approve/<id>` on the gateway; denial
  uses the existing DELETE path. There is no UI to revoke or edit
  an existing file-sharing grant -- the user has to edit
  `latchkey_permissions.json` by hand for that, for now.
* `LatchkeyGatewayClient` gains an `approve_permission_request`
  method. The `StreamedPermissionRequest` model carries the new
  wire shape (`request_type` + `payload` + `target` + `effect`).
  `payload` is typed directly as the `PredefinedRequestPayload |
  FileSharingRequestPayload` union (pydantic's smart-union mode
  resolves the two disjoint shapes at decode time), and `effect` is
  typed as a `PermissionEffect` model with `rules` and `schemas`
  fields. Consumers dispatch via `isinstance` on `payload` rather
  than re-validating the dict at the call site.

The Minds REST API ships a new WebDAV file-server mount at
`/api/v1/files`, backed by [`wsgidav`](https://wsgidav.readthedocs.io/)
wrapped in [`a2wsgi`](https://github.com/abersheeran/a2wsgi). Two
share roots are exposed:

* the current user's home directory (`Path.home()`); and
* `/tmp`.

Each share is mounted at its on-disk path so the outward URL mirrors
the absolute path one-to-one: `/home/<user>/foo.txt` is reached at
`/api/v1/files/home/<user>/foo.txt`, `/tmp/blob.bin` at
`/api/v1/files/tmp/blob.bin`. Any standard WebDAV verb works (`GET`,
`PUT`, `PROPFIND`, `DELETE`, ...); paths outside the two shares are
not served. The HTML directory browser is disabled.

The mount uses the same per-agent Bearer-token authentication as the
rest of `/api/v1/`: a thin ASGI wrapper verifies
`Authorization: Bearer <api_key>` against `find_agent_by_api_key` and
401s before any request reaches the filesystem; WsgiDAV itself runs
anonymous. The mount is reachable from agents through the
`minds-api-proxy` Latchkey extension.

`MINDS_API_KEY` is now written to the workspace host's env file via
`--host-env` (instead of the system-services agent's per-agent env via
`--env`) when running `mngr create` for a new workspace. Each workspace
now spawns multiple agents on the same host (the initial
`system-services` agent plus the chat agents the FCT bootstrap and the
system_interface's "New Chat" button create), and only the
system-services agent's `mngr create` is invoked by minds itself. Moving
the variable to the host env file lets every agent on the host inherit
the same key, so chat agents can authenticate against the desktop
client's `/api/v1/...` routes (including the new file-sharing endpoints)
just like the system-services agent. The API-key hash is still stored
once under the system-services agent's id, so all workspace-side
requests resolve to that id for caller identification.

## New providers panel on the landing page

- The landing page now includes a Providers section listing every configured provider (except `local`, which is always present and always healthy). Each entry shows the provider name, backend type, a status badge (OK / Error / Disabled), the last error message verbatim when applicable, and an Enable or Disable button.
- Two small freshness counters at the top of the panel show "time since last discovery event" and "time since last full discovery event" so a stalled discovery loop is immediately visible.
- Clicking Disable on a working or errored provider, or Enable on a disabled one, writes `is_enabled` to minds' active settings file and bounces `mngr observe` so the change takes effect on the next poll. The button shows "Waiting…" until the next full snapshot lands.

## No more silent auto-disable on auth errors

- Previously, when discovery surfaced `ImbueCloudAuthError`, minds would silently rewrite the user's settings to set `is_enabled = false` on the offending `imbue_cloud_<slug>` provider. That entire path is gone: `_ImbueCloudAuthErrorDisabler` and the provider-error callback plumbing on `EnvelopeStreamConsumer` are removed.
- The same outcome is now user-driven: an errored `imbue_cloud_<slug>` provider shows up in the providers panel with the verbatim error message; the user clicks Disable to silence it, or fixes the upstream auth and the provider recovers on the next snapshot.

## Agents no longer silently disappear when a provider fails

- When a provider (e.g. Modal or imbue_cloud) fails discovery, its agents previously vanished from the landing page agent list with no explanation. Now `AgentObserver` emits an `UNKNOWN` agent state for previously-observed agents on errored providers (sticky until they reappear or are explicitly destroyed). The landing page's agent list itself still shows only currently-discovered agents, but the providers panel surfaces the underlying provider error so the user can see *why* an agent might be missing.
- `mngr_notifications` users: see that project's changelog for the new `RUNNING -> UNKNOWN -> WAITING` transition handling.

## Internal: `set_provider_is_enabled`

- `disable_imbue_cloud_provider_for_account` was renamed to `set_provider_is_enabled(provider_name, is_enabled)` and generalized to work on any provider name. All callers in `apps/minds/` are migrated; no compatibility shim. The function writes to minds' active settings file and creates the `[providers.<name>]` block if it doesn't yet exist.

## 2026-05-21

Adds `test_create_local_docker_workspace_via_electron`: an acceptance test that drives the real Electron minds app via Playwright over CDP, clicks through the create form, waits for the workspace's `system_interface` dockview UI to render through the desktop client proxy, and cleans up the resulting `mngr` agent. Resolves the forever-claude-template source in three steps -- a local `.external_worktrees/` worktree first, then a shallow clone of the matching mngr branch on the FCT public remote, then `main` -- so the test runs unchanged in CI and against an operator's local FCT working tree.

Adds the `MINDS_MNGR_FORWARD_PORT` env var to `minds run` so test harnesses (and concurrent `just minds-start` invocations) can dodge the hardcoded default port 8421 collision.

Replaces the stale skipped `test_create_agent_e2e` (which never drove Electron and carried an out-of-date "TUI send-enter timeout" skip reason that no longer applies after FCT split its services agent from its chat agent).

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

- Add a new `ci` tier to the minds env system (alongside `dev`/`staging`/`production`). `ci-<...>` env names are now accepted everywhere `dev-<...>` names are; the new tier mirrors the dev tier's lifecycle (per-env Modal env, per-env Neon project + SuperTokens app, per-env local state) and reads its Vault secrets from `secrets/minds/ci/*` (mirrored from `secrets/minds/dev/*` for now).
- The deployment-tests orchestrator now mints ephemeral envs named `ci-<timestamp>-<uuid>` (was `dev-ci-<...>`); shared envs are now `ci-<run-id>` (was `dev-ci-<run-id>`). The shorter names stay within Modal's DNS-label budget with more headroom.

`minds env activate` no longer exports `MODAL_PROFILE` by default.
Activation now has two modes:

- **Use-only (default)**: `minds env activate <name>` exports the four
  use-side env vars (`MINDS_ROOT_NAME`, `MNGR_HOST_DIR`, `MNGR_PREFIX`,
  `MINDS_CLIENT_CONFIG_PATH`) and emits `unset MODAL_PROFILE`. This is
  what every non-deploying user wants -- the desktop client, mngr, and
  Latchkey no longer try to authenticate against a Modal workspace the
  operator may not have tokens for. Fixes the spurious "Modal is not
  authorized" warnings + Latchkey breakage that hit anyone running
  `minds run` after `eval "$(uv run minds env activate staging)"`
  without a `minds-staging` profile in `~/.modal.toml`.
- **Deploy-mode (`--deploy`)**: `minds env activate --deploy <name>`
  additionally exports `MODAL_PROFILE=<tier's modal_workspace>` and
  pre-validates that `~/.modal.toml` has a matching profile (fails up
  front with a `modal token set --profile <workspace>` hint when it
  doesn't, instead of letting downstream `modal …` shellouts surface
  the auth error).

`minds env deploy`, `minds env destroy`, and `minds env recover` now
refuse to run unless the shell is deploy-activated (`MODAL_PROFILE`
must equal the tier's `modal_workspace`). The refusal message tells
the operator the exact `eval "$(uv run minds env activate --deploy
<name>)"` to run.

The packaged Electron app and `deployment_tests/helpers.py` are
unchanged -- both set their Modal credentials independently of shell
activation.

## 2026-05-20

- The "Creating your project" page now updates its spinner caption as the setup progresses ("Starting...", "Cloning repository...", "Checking out branch...", "Provisioning AI access...", "Creating workspace...", "Waiting for workspace to be ready..."), instead of staying on "Cloning repository..." through the whole flow. Phase state is now carried on the existing ``AgentCreationStatus`` enum as the single source of truth -- the spinner caption is resolved from that enum value by the SSE stream, which polls the creation status on each loop iteration. The ``/api/create-agent/{id}/status`` JSON API now returns the new enum values (``INITIALIZING``, ``CLONING_REPO``, ``CHECKING_OUT_BRANCH``, ``PROVISIONING_AI``, ``CREATING_WORKSPACE``, ``WAITING_FOR_READY``, ``DONE``, ``FAILED``) instead of the previous ``CLONING`` / ``CREATING``.

- `just minds-start` now unsets `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` before launching the desktop client, so credentials exported in the developer's shell no longer leak into agents created by the dev app.

Renamed the "workspace server" feature to "system interface" in the desktop client: the menu item / recovery page label "Restart workspace server" became "Restart system interface". Frontend Electron clients automatically pick up the new wire format and labels.

Workspace-server restart and health-recovery UI on the `mngr_forward` plugin architecture.

User-visible changes:

- When an agent's workspace server stops responding, the chrome auto-navigates the workspace view to a recovery page where the user can restart the server. The recovery page streams server-status updates over SSE and reloads back to the workspace once the server is healthy again.
- The landing page now annotates each project row with a status badge when its workspace server is unresponsive or restarting; clicking such a row goes to the recovery page instead of the workspace.
- The sidebar context menu gained a "Restart workspace server" entry that opens the recovery page for the selected workspace.
- A dedicated recovery page (`/agents/<id>/recovery`) renders the restart button, streams server-status updates via SSE, and auto-reloads back to the workspace once the server is healthy again.
- Minds tracks `workspace_backend_failure` envelopes from the `mngr_forward` plugin as a per-agent state machine (HEALTHY -> STUCK after 5 seconds of continuous failures -> RESTARTING during a user-triggered restart -> back to HEALTHY on the first successful probe).

Restart UX improvements on top of the above:

- The `/api/agents/<id>/restart-workspace-server` endpoint now returns 200
  as soon as the `mngr exec` kill dispatch completes (it no longer blocks
  for up to 15 seconds polling the workspace through the plugin). The
  background workspace-health probe loop continues to flip the tracker back
  to HEALTHY once the workspace is responsive. This makes the endpoint a
  reliable "the workspace has been killed" signal for callers that want to
  navigate to the plugin's loader page.
- The recovery page's "Restart workspace server" button and the sidebar
  right-click "Restart workspace server" menu item now both await the
  restart API response before navigating to the workspace URL. Previously
  they fired the POST and navigated immediately, which on a still-healthy
  workspace raced against the in-flight kill and silently reloaded onto
  the unchanged iframe. Awaiting guarantees the user lands on the plugin's
  "Workspace server starting..." loader.
- The recovery page now notes that running agents are not interrupted by a
  workspace-server restart.
- Stale failure envelopes arriving immediately after a successful restart
  no longer cause a brief recovery-page flash; the health tracker now
  ignores failures within a short grace window after recovery.
- The "Workspace server starting" loader spinner no longer visibly jumps
  on each refresh. The spinner's animation duration now matches the page's
  1-second auto-refresh interval, so the spinner is at the cycle boundary
  (rather than 90 degrees past it) when the reload fires.

Minds: start the latchkey gateway client lazily on a background thread so `minds run` no longer blocks on the `mngr latchkey forward` supervisor binding its gateway port. Callers that need the gateway (the permission-request stream consumer and the FastAPI request handlers) wait on `ensure_initialized()` themselves the first time they use the client.

- The minds desktop client has been adapted to the new latchkey
  permission-request shape: `LatchkeyPermissionRequestEvent` now carries
  `scope` (Detent schema) and `permissions` (the agent's requested list)
  instead of `service_name`. The previously-bundled
  `apps/minds/imbue/minds/desktop_client/latchkey/services.toml` has
  been deleted; the desktop client now lazily fetches the catalog from
  the gateway's `/permissions/available` endpoint (cached in process)
  to look up display names and the legal permission set. The grant
  dialog continues to render the display name ("Slack" etc.) and lets
  the user broaden or narrow the requested permission set.
- The minds desktop client now tolerates legacy response events on
  disk. Older versions wrote a ``service_name`` field on each
  ``RequestResponseEvent``; the current schema replaced it with
  ``scope``. Without a migration the historical events.jsonl emitted
  a pydantic-extras warning per legacy line at every minds startup
  and the corresponding request would not be marked resolved. The
  loader now drops ``service_name`` before validating, so historical
  responses load cleanly and their requests are correctly filtered
  out of the pending list. The dropped ``service_name`` is
  informational only -- pending-request filtering uses
  ``request_event_id`` -- so no functional information is lost.
- The streamed-permission-request handler now dedupes redeliveries by
  ``event_id``. The gateway re-emits every still-pending request on
  each stream reconnect (every couple of seconds when idle), but the
  handler used to append a fresh entry to the in-memory request inbox
  and emit an INFO log line + an SSE wake-up for every redelivery. The
  ``requests`` list therefore grew unbounded for as long as a request
  stayed pending, and the desktop log filled with duplicate ``Streamed
  latchkey permission request ...`` lines. The handler now checks the
  inbox for the incoming ``event_id`` first and no-ops on a match.
- Fixed a startup race where the minds desktop client could cache a
  stale latchkey gateway port and then fail every subsequent call
  with ``[Errno 111] Connection refused``. The race occurred because
  the supervisor restart and the gateway-client pre-warm previously
  ran on independent background threads at minds startup: the
  gateway client could observe the previous supervisor's record
  (still on disk, still alive) before the restart deleted that
  record and stamped the fresh port. Two fixes:
  - ``LatchkeyGatewayClient`` now self-heals from a stale cached
    gateway URL on connect-level transport failures
    (``httpx.ConnectError`` / ``httpx.ConnectTimeout``): the cached
    URL is invalidated and the next call re-resolves the port from
    the supervisor's on-disk record. Non-connect errors (read
    failures mid-stream, 5xx responses, etc.) continue to propagate
    without invalidation, since those usually indicate a problem at
    the gateway end rather than a stale local cache.
  - The supervisor restart and the gateway-client pre-warm now run
    sequentially on a single background thread, eliminating the
    race in the first place. App startup is unaffected: this still
    runs in a background thread, so the supervisor restart's 10s
    SIGTERM grace never blocks the foreground startup path.
- The latchkey permission dialog no longer pre-checks the catch-all
  ``any`` permission as an implicit default. ``any`` is still offered
  as the first checkbox so the user can opt into unrestricted access
  explicitly, but the initial check state is now the union of (a)
  permissions already granted for the scope on the agent's host and
  (b) the permissions the agent declared in the request event.
  Approving without modification therefore grants exactly that union
  (matching the user's mental model of "give the agent what it's
  asking for, on top of what it already has"). Previously, existing
  grants alone seeded the pre-check and the agent's new ask was
  ignored unless the user actively ticked it; under the new behavior
  an unmodified Approve actually delivers the requested permissions.

Update `apps/minds/docs/staging-bringup.md`'s changelog-entry checklist item to reflect the new per-project layout (`changelog/minds/<branch-name>.md` instead of `changelog/<branch-name>.md`).

Batch of `minds env deploy` / connector follow-ups from the F-numbered
findings in `MANUAL_DEPLOY_FINDINGS.md`:

- ``minds env deploy``'s post-deploy health check now polls the connector's
  new ``GET /health/liveness`` route instead of ``/docs`` (smaller, faster,
  symmetric with the LiteLLM proxy's existing liveness probe). The
  per-attempt HTTP timeout bumped from 3s to 10s and the total budget
  from 30s to 60s so cold-booting Modal containers have a realistic
  chance to respond before being declared unhealthy. (F2, F3)
- ``DeployLifecycleConfig`` has a new pydantic model validator that
  rejects ``writes_local_state=true`` + ``creates_resources=false``
  at deploy.toml parse time. The combination would previously have
  AssertionError'd partway through deploy AFTER both Modal apps had
  been deployed; failing at config load is far less surprising. The
  matching asserts in ``deploy_env`` stay as defense-in-depth for
  non-CLI callers. (F18)
- ``minds env deploy`` runs ``apply_pool_hosts_migrations`` for every
  tier instead of only the dev tier. Shared tiers (staging /
  production) source the host_pool DSN from ``DATABASE_URL`` in their
  operator-managed ``secrets/minds/<tier>/neon`` Vault entry. Without
  this, a new ``.sql`` migration shipped via PR would apply to dev
  envs immediately but never to staging / production until the
  operator ran psql manually -- and the two schemas would diverge.
  (F17)
- ``minds env destroy`` proceeds with cloud-side cleanup even when the
  local env root has already been removed by hand. The cloud-side
  resources are keyed off the env *name*, not the local directory, so
  an operator who ``rm -rf``'d ``~/.minds-<env>/`` can still re-run
  destroy by name to clean up Modal apps / Neon / SuperTokens /
  Cloudflare tunnels / OVH instances. ``destroy_env`` no longer
  raises ``DevEnvNotFoundError`` for missing-root; it logs a warning
  and proceeds. Step 1 (``mngr destroy`` per agent) becomes a no-op
  since the agents directory is gone too. (F22)
- ``per_env_connector_url`` / ``per_env_litellm_proxy_url`` now take
  the ``tier`` as a keyword arg. The dev URLs stay shaped as
  ``rsc-dev`` / ``llm-dev`` so existing per-env deployments keep
  working without a redeploy, but any future ``PER_ENV`` tier other
  than dev gets the right ``rsc-<tier>`` segment automatically
  instead of silently colliding on the hardcoded ``dev`` segment.
  (F24)
- ``minds env deploy``'s ``find_monorepo_root`` check happens BEFORE
  the Vault credential read in the CLI and BEFORE
  ``make_deploy_id`` inside ``deploy_env``. Running from outside
  the monorepo now fails immediately with a clean error rather than
  reading Vault first and logging a misleading "Deploy id: ..."
  line. (F15)
- ``minds env list`` resolves the reserved tiers' (``production`` /
  ``staging``) client.toml to the committed in-repo
  ``apps/minds/imbue/minds/config/envs/<tier>/client.toml`` instead
  of showing ``(no client.toml)``. The ``DevEnvSummary`` gains a
  ``client_config_source: "env_root" | "in_repo" | None`` field so
  machine consumers can distinguish "per-env file" from "in-repo
  file" from "unprovisioned." Human-format output now reads
  ``<path>  (in-repo, committed)`` for reserved tiers and
  ``(no client.toml -- run `minds env deploy`)`` for unprovisioned
  dev envs. (F11)
- The ``litellm-connector`` Modal Secret no longer appears in
  ``[secrets].services`` -- it was never vault-backed (no
  ``secrets/minds/<tier>/litellm-connector`` Vault entry exists),
  and the carve-out (``_DERIVED_ONLY_SECRET_SERVICES``) that
  suppressed a misleading per-deploy warning was a code smell. The
  deploy now pushes the secret as a separate code-driven step at
  the end of the secret-push loop; ``_DERIVED_ONLY_SECRET_SERVICES``
  is deleted. ``[secrets].services`` in every tier's deploy.toml
  becomes a truthful "vault-backed only" list. The post-deploy GC
  picks up ``litellm-connector-<tier>-<deploy_id>`` secrets via the
  same suffix-match pattern, so no GC bookkeeping changes. (F25)
- Recover changes: when the captured pre-deploy app version is
  ``None`` (a first-ever deploy of this env / tier), ``minds env
  recover`` now ``modal app stop``s the deployed app instead of
  leaving it running -- otherwise the just-deleted Modal Secrets
  would leave the app 500'ing on every request. (F19)
- The recover-target file is per-env: each in-flight deploy gets
  its own ``.minds-deploy-recover-target-<env>.json`` at the
  monorepo root, so concurrent deploys against different envs don't
  refuse each other (useful for parallel test runs). The
  environment-scoped commands (``deploy`` / ``destroy``) refuse only
  if THEIR env's file exists; the env-agnostic commands (``activate``
  / ``deactivate`` / ``list``) still refuse loud if ANY recover-
  target file exists (listing all in the error). ``deploy_env`` and
  ``recover_env`` additionally hold a per-env ``flock`` on
  ``.minds-deploy-lock-<env>.lock`` for their entire process
  lifecycle, so two concurrent invocations against the same env
  serialize at the kernel level. (F26)
- Doc / spec updates: comment on the connector's ``/generation`` env
  var now explains empty-string is the steady state for
  ``tracks_generation=false`` tiers (not a legacy artifact). Spec
  ``specs/minds-deploy-safety-overhaul/spec.md`` updated to use
  branch-based Neon-snapshot terminology (the implementation pivoted
  from the spec's original named-restore-point design because Neon's
  named-restore-point API is org-tier-gated) and to refer to
  ``/health/liveness`` on both apps. (F1, F4, F9, F20)

Minds dev-environment fixes:

- Hard-enforces the `dev-<your-user>` naming convention for dev envs:
  `DevEnvName` rejects anything that does not start with `dev-`, and
  `MINDS_ROOT_NAME_PATTERN` only accepts `minds`, `minds-staging`, or
  `minds-dev-<rest>`. Dev env roots come out tier-first as
  `~/.minds-dev-<your-user>/` and `MINDS_ROOT_NAME=minds-dev-<your-user>`.
- `minds env activate` now exports `MODAL_PROFILE` derived from the
  activated tier's committed `modal_workspace`. Every subsequent
  `modal` CLI shellout (deploy, secret create, environment create) is
  pinned to the right workspace regardless of which profile is marked
  `active = true` in `~/.modal.toml`. Prerequisite: the operator must
  have a matching profile in `~/.modal.toml` for each tier
  (`modal token set --profile <workspace>` once per tier). Skipped
  when the tier's `modal_workspace` is still the literal `CHANGE_ME`
  placeholder.
- `min_containers` for the deployed `remote-service-connector-<tier>`
  and `litellm-proxy-<tier>` Modal apps is now driven by a tier's
  committed `deploy.toml` via a new `[min_containers]` block (fields:
  `connector`, `litellm_proxy`). Defaults to 0 in the Pydantic model;
  staging / production deploy.toml ship with `1` for both. The values
  thread into `modal deploy` as `MINDS_CONNECTOR_MIN_CONTAINERS` /
  `MINDS_LITELLM_PROXY_MIN_CONTAINERS`, which the modal app modules
  read at import time.
- Per-dev-env Neon **project** (not just a database): each dev env
  now owns a brand-new Neon project named `minds-<env>` under the
  dev-tier Neon org, containing two databases (`host_pool` and
  `litellm_cost`). `minds env deploy` provisions the project and
  applies the `pool_hosts` schema (via `apps/remote_service_connector/
  migrations/*.sql`) to `host_pool` automatically. `minds env destroy`
  deletes the project outright -- atomic teardown of both DBs, roles,
  and the project's pooler endpoint.

  The deploy now overrides BOTH `neon.DATABASE_URL` and
  `litellm.DATABASE_URL` in the per-env Modal Secrets with the per-env
  project's two DSNs, so the connector and the LiteLLM proxy talk to
  the same env-isolated Neon project. The per-env `secrets.toml` on
  disk grows two fields (`NEON_HOST_POOL_DSN`, `NEON_LITELLM_DSN`,
  replacing the single `NEON_POOLED_DSN`).

  Vault `secrets/minds/<tier>/neon-admin` now expects `NEON_ORG_ID`
  (instead of `NEON_PROJECT_ID`). The token must have project-create
  scope on the dev tier's Neon org.

  `mngr imbue_cloud admin pool create` and friends now auto-resolve
  `--database-url` from the activated minds env's `NEON_HOST_POOL_DSN`
  (or `MINDS_HOST_POOL_DSN` env var), so the standard dev-env flow no
  longer requires passing the DSN explicitly. Operators outside an
  activated env still pass `--database-url` directly.

  Staging / production keep the tier-shared single-DB model unchanged.

- Added a `secrets/minds/<tier>/ovh` Vault template (AK / AS / CK) and
  documented the manual provisioning step in
  `apps/minds/docs/vault-setup.md` and
  `apps/minds/docs/host-pool-setup.md`.

- `minds env deploy` is now actually idempotent against Neon. The
  Neon REST API does not 409 on duplicate project names within an
  organization -- POSTing `/projects` with a name that's already in
  use creates a second, distinct project with the same name and a
  different id. The previous `create_neon_project` assumed Neon would
  409 (the adopt-fallback path was never reached), so every dev-tier
  re-deploy silently leaked an entire Neon project (with its own
  host_pool + litellm_cost DBs + branches + endpoints). Several
  attempts at deploying dev-josh-1 during one session today left
  four projects named `minds-dev-josh-1` in the dev org. The same
  bug would have caused `minds env destroy` to delete the wrong
  project (always the first match from the list endpoint, i.e. the
  oldest, not the live one), leaving the live project stranded.
  `create_neon_project` and `delete_neon_project` now look up by
  name first via `_find_projects_by_name`, adopt when there's
  exactly one match, raise a `NeonProviderError` with every
  matching project id + creation timestamp + a copy-pasteable
  cleanup recipe when there are several. Refusing-loud is
  intentional: silently picking one would risk destroying the wrong
  project under a real name collision (e.g. two devs using the same
  env name cross-machine). A new `_select_one_or_raise_multi_match`
  pure helper carries the decision logic; the operator-facing error
  message is unit-tested.

Minds deploy safety overhaul (spec
`specs/minds-deploy-safety-overhaul/spec.md`):

- Shorter Modal app + function names so the deployed hostname stays
  under DNS's 63-char limit for every realistic env name:
  `remote-service-connector` -> `rsc`, `fastapi_app` -> `api`,
  `litellm-proxy` -> `llm`, `litellm_app` -> `proxy`. Modal workspaces
  rename to `minds-dev` / `minds-staging` / `minds-production`. URL
  is now exactly what we compute up front, so the deploy no longer
  runs a second-pass secret push or a connector redeploy. `DevEnvName`
  enforces a 40-char max so the hostname budget always fits.

- One `minds env deploy` path for every tier, driven by a new required
  `[lifecycle]` block in each tier's `deploy.toml` (flags:
  `creates_resources`, `modal_env_strategy`, `writes_local_state`,
  `tracks_generation`). dev / staging / production all execute the
  same code now; behavior diverges only via the flag matrix.
  `deploy_dev_env` + `deploy_tier_env` collapse into `deploy_env`.
  Inline best-effort rollback machinery (`_best_effort_rollback`,
  `_ROLLBACK_TABLE`, `_rollback_*`) deleted -- replaced by
  `minds env recover` (below). Production now `tracks_generation=true`
  for parity with staging (production destroy is hard-refused so the
  generation id is effectively immutable for the tier's lifetime).

- Pool-hosts schema migrations now backed by a real
  `schema_migrations(version, applied_at)` table instead of the old
  "replay every .sql with IF NOT EXISTS guards". New
  `apps/minds/imbue/minds/envs/migrations.py` owns the runner. Legacy
  files keep their `IF NOT EXISTS` guards so a backfill against an
  already-migrated DB is a no-op + records the row; new migrations
  land WITHOUT guards (the table is the source of truth).

- Every `minds env deploy` mints a fresh `MINDS_DEPLOY_ID` (UTC
  `YYYYMMDDTHHMMSSZ`) and pushes every Modal Secret under a new name
  `<svc>-<tier>-<deploy_id>` (no overwrites). The deployed Modal apps
  read `MINDS_DEPLOY_ID` at module load and pin every
  `Secret.from_name(...)` to the matching timestamped name. Hard-fails
  at module load if `MINDS_DEPLOY_ID` is missing (no fallback to
  unsuffixed names; manual `modal deploy` outside `minds env deploy`
  is unsupported). End-of-deploy GC keeps the last 10 timestamped
  secrets per `<svc>-<tier>`; shared-tier destroy deletes all
  timestamped secrets matching the tier.

- New `minds env recover` command + recover-target file at the
  monorepo root. Every deploy captures pre-deploy Modal app versions,
  creates a Neon snapshot branch (`pre-deploy-<deploy_id>` off the
  default branch -- COW so it's near-free), and writes
  `.minds-deploy-recover-target.json` (gitignored) atomically BEFORE
  touching any external state. On a failed deploy, the operator runs
  `minds env recover`; it idempotently runs every reversal step
  (`modal app rollback`, Neon branch-restore from the snapshot with
  the pre-restore state preserved under `pre-rollback-<deploy_id>`,
  delete orphan timestamped secrets, delete the recover-target file).
  Successful deploys delete the snapshot branch (best-effort cleanup).
  Every other `minds env *` command (`activate` / `deactivate` /
  `list` / `deploy` / `destroy`) refuses to run while a recover-
  target file exists.

  Snapshot/restore works for every tier (dev creates_resources=true
  and shared creates_resources=false). Shared tiers (staging /
  production) require `NEON_PROJECT_ID` in their
  `secrets/minds/<tier>/neon-admin` Vault entry; the deploy resolves
  the default branch on demand. Without `NEON_PROJECT_ID` shared-tier
  deploys log a warning and skip the snapshot (so recover can roll
  back Modal apps but not the DB).

- Post-deploy health check: `await_apps_healthy` polls
  `<connector>/docs` and `<litellm_proxy>/health` for up to 30s each
  (sequential), with cold-boot 5xx tolerance + immediate failure on
  4xx / 5xx-with-body / wrong-shape responses. Failure surfaces as
  `HealthCheckFailedError` and goes through the same "run
  `minds env recover`" guidance as any other deploy failure.

- Each deploy also gets a `[lifecycle].tracks_generation=true` tier
  generation id minted into the litellm-connector Modal Secret (no
  change for dev / staging; production now also gets one).

Operator-visible: re-deploys after any of the above are
backwards-compatible against the existing dev-tier resources. The
shared (`staging` / `production`) tiers' `deploy.toml` files now
require a `[lifecycle]` block; operators bringing up staging /
production for the first time need to populate the existing OAuth
client IDs as before plus ensure the `[lifecycle]` block matches the
defaults documented in the committed file.

Speed up local minds workspace creation by restructuring the `forever-claude-template` Dockerfile and deferring Playwright into a post-boot install. The bulk of this change lives in the `forever-claude-template` repo (see `mngr/faster-minds-build` over there); this monorepo PR carries the spec (`specs/faster-minds-build/concise.md`) and a one-line mention in `apps/minds/docs/design.md`.

What changes for end users:

- Cold (no Docker layer cache) image builds drop the Playwright + Chromium install from the Dockerfile entirely. That step was downloading ~280 MB of browser assets plus apt-installing system libraries on every cold build; it now runs once on first container boot via a new `deferred-install` service.
- Warm-cache rebuilds after a code-only edit (no manifest changes) no longer invalidate the heavy `uv sync --all-packages` and `npm ci` layers. The Dockerfile now copies dependency manifests in an early layer, runs `uv sync --frozen --no-install-workspace --no-install-local` to pre-warm the wheel cache + `npm ci` for the frontend, and only then does `COPY . /code/`. Post-`COPY` `uv sync` collapses to ~1.5s because the warmed cache covers every third-party wheel; `npm run build` similarly reuses cached `node_modules`.
- Drop the post-`COPY` recursive `chown -R root:root /code/` step. `COPY` without `--chown` already lands files as root:root, so the chown was a no-op walk over the entire (~250 MB, including `.git/`) source tree -- worth ~60s on every warm-cache rebuild. Measured warm-rebuild (single Python edit, all pre-`COPY` layers cached): **1m33s -> 30s**.
- Drop `mngr_modal` from the post-`COPY` `uv tool install -e apps/system_interface --with-editable ...` chain and from `mngr plugin add --path ...`. The FCT `.mngr/settings.toml` sets `providers.modal.is_enabled = false` and no Python in `apps/` or `libs/` imports `imbue.mngr_modal`, so the plugin was load-bearing for nothing. `mngr plugin add` shells out to a uv-tool inject per plugin, so trimming one plugin saves a measurable amount. Brings warm rebuild to **~25.6s** total.
- Playwright's Chromium browser installs asynchronously on first boot via a new `services.toml` entry `deferred-install` (running `scripts/deferred_install.sh`). The script is idempotent: per-package marker files under `/var/lib/minds/deferred-install/done.<package>` gate every install, so subsequent container restarts no-op in milliseconds and packages never silently upgrade between restarts. Container rebuilds wipe the marker so the install runs exactly once on a fresh image.
- The `forever-claude-template` `.dockerignore` is now a symlink to `.gitignore` (Docker reads the symlink target). `.gitignore` patterns were rewritten to start with `**/` (or contain a path separator) so the same patterns work in both formats; two new ratchets in `test_meta_ratchets.py` (`test_gitignore_patterns_use_double_star`, `test_dockerignore_is_symlink_to_gitignore`) keep the convention enforced.

If a process tries to use Playwright before the deferred install has finished, it will fail loudly -- that is acceptable. `forever-claude-template/CLAUDE.md` documents how to check the marker file or the `svc-deferred-install` tmux window before exercising browser automation in a fresh workspace.

Out of scope for this PR (kept for follow-ups): BuildKit cache mounts for the `uv` / `npm` wheel caches across image rebuilds; pulling the same restructuring into the lima provider's `.mngr/settings.toml` `create_templates.lima.extra_provision_command`; deferring other "nice but not required" packages (e.g. `modal` CLI, apt convenience tools); generalizing the deferred-install marker pattern into a small framework.

End-to-end fixes for the OVH-backed imbue_cloud pool flow (`minds pool create` -> `mngr imbue_cloud admin pool create` -> bake -> lease/adopt -> first-start). Discovered + fixed iteratively while smoke-testing the flow against a fresh dev env (`dev-josh-ovh`).

### `minds pool create` auto-injects tier secrets

- `minds pool create` reads the activated tier's OVH AK/AS/CK from Vault (`<vault_path_prefix>/ovh`) and injects them into the inner `mngr imbue_cloud admin pool create` subprocess. Operators no longer need to export `OVH_APPLICATION_KEY` / `OVH_APPLICATION_SECRET` / `OVH_CONSUMER_KEY` before baking pool hosts; activating a minds env is sufficient. Vault values win over any stale `OVH_*` in the shell so a session left over from a different tier's bake can't silently misroute the OVH order.
- `--management-public-key-file` is now optional. Default behavior derives the public key from the activated tier's `<vault_path_prefix>/pool-ssh.POOL_SSH_PRIVATE_KEY` Vault entry -- the same private key the deployed connector loads from its `pool-ssh-<tier>` Modal Secret. Closes the keypair-mismatch class of bakes that succeeded locally but failed every subsequent lease with "Authentication failed" at the connector's SSH-key-injection step (the operator's hand-rolled pub key didn't match the connector's stored priv key). The flag stays available as an operator escape hatch for one-off / non-vault setups.

Deploy-safety overhaul: three correctness fixes to `_deploy_env_locked` discovered while auditing PR #1671 (full audit in `DEPLOY_SAFETY_AUDIT.md`).

- **F1**: Neon snapshot + recover-target file write now happen BEFORE pool-hosts migrations run. Previously migrations ran first, so the snapshot captured the post-migration state and `minds env recover` could not roll back a bad migration -- especially dangerous for shared tiers (staging/production) where the operator-managed DB likely has live traffic. The new ordering: capture app versions → resolve Neon project → verify token scope (F2) → snapshot → write recover-target (with F4 cleanup-on-failure) → migrations.
- **F2**: `providers.verify_neon_token_has_restore_scope(...)` is now actually called as a preflight check, right after Neon project resolution and before snapshot creation. It was declared on the Providers bundle and wired to the real implementation but had zero callers in the deploy path. Stale/misconfigured Neon tokens now fail at the cheapest possible probe (a `GET /projects/{id}` call) before any mutation, instead of only surfacing at `minds env recover` time after the deploy had already started rolling forward.
- **F4**: `write_recover_target_atomic` is now wrapped in a `try/except (OSError, MindError)` that best-effort deletes the just-created Neon snapshot branch before re-raising. Closes a window where a successful snapshot followed by a failed local file write (disk full, ENOSPC, permission denied, fsync failure) would orphan the snapshot branch with no `recover-target` file pointing at it. Cleanup failure is logged loudly so the operator knows the branch needs manual deletion; the original write error still propagates as the user-visible exception.

Each fix has two new ratchet tests in `provisioning_test.py` pinning the invariant (snapshot-before-migration for dev + shared tier; verify-before-snapshot happy path + short-circuit on scope failure; snapshot cleanup on write failure + on compounded cleanup failure).

Spec + scaffolding: design and initial wiring for live integration / acceptance / release testing of the minds app, its deployed remote services, and the deployment process itself. Introduces an operator-invoked `just minds-test-deployment` orchestrator (plain-Python click CLI) that stands up shared dev envs and runs two pytest batches strictly sequentially via local `uv run pytest` (one per mark: `minds_deployment`, `minds_services`), and reliably cleans up every resource it creates via both a per-run ledger and a `ci-<timestamp>` name+age sweep. Offload-Modal parallelism is designed in but deferred to a follow-up. See `specs/minds-deployment-tests.md` for the full design.

`minds env deploy` now picks the Modal deploy strategy (rollover vs recreate) from context, with operator overrides via `--hard` / `--soft`. Default policy: recreate when a migration ran or the target tier is `dev` (covers personal dev envs + CI ephemeral envs), rollover for staging / production with no migration. Adopts Modal's `--strategy=recreate` flag from 1.4.x so the warm prior-version container no longer keeps serving traffic for several minutes after the swap on dev-tier deploys.

- Added `apps/minds/docs/staging-bringup.md`, an end-to-end checklist
  for standing up the `staging` minds tier from scratch (cloud
  account creation, Vault population, first-time `minds env deploy
  --yes-i-mean-staging`, and local smoke-test against the new tier).

Swap the `minds env destroy` walker from Vultr to OVH:

- New top-level `minds pool` CLI group (`create` / `list` / `destroy`). It requires an activated minds env, auto-injects `--tag minds_env=<active-env>`, and shells out 1:1 to `mngr imbue_cloud admin pool ...`.
- `minds env destroy` swaps its Vultr `/instances` walker for an OVH IAM v2 walker (matches by `tags["minds_env"] == <env>` and terminates via `OvhVpsClient.destroy_instance`). The dev-tier Vault path is now `<tier>/ovh` with `OVH_APPLICATION_KEY` / `OVH_APPLICATION_SECRET` / `OVH_CONSUMER_KEY`.

The orphaned `apps/minds/imbue/minds/cli/pool.py` duplicate (pre-`mngr_imbue_cloud`) and `apps/minds/imbue/minds/envs/providers/vultr_tags.py` are deleted in the same change. Existing Vultr-backed `pool_hosts` rows are not migrated automatically; operators destroy / drop them by hand after merge.

Move minds to multi-environment deploys (`dev`, `staging`, `production`) backed by HCP Vault, and reshape every env around a per-env data root. Each env now owns one directory: `~/.minds/` for production and `~/.minds-<env-name>/` for every other env (staging, plus any per-developer dynamic dev env). Each root holds that env's own mngr profile, agents, auth, logs, and (for dev envs) a split chmod-0600 `secrets.toml` next to a public `client.toml`. The pre-refactor shared `~/.devminds/` layout is gone -- `rm -rf ~/.devminds/` when convenient. `MINDS_ROOT_NAME` validation tightens to `minds(-<env-name>)?`; legacy values like `devminds` are silently treated as unset with a warning so a stale shell falls back to production rather than blowing up.

`minds env` is reorganized around explicit shell activation. New `minds env activate <name>` exports `MINDS_ROOT_NAME` + the derived `MNGR_*` vars + `MINDS_CLIENT_CONFIG_PATH` for `eval` (staging/production point at the in-repo committed `client.toml`; dev envs at the per-env `~/.minds-<name>/client.toml`); new `minds env deactivate` unsets them. A `--create` flag on `activate` idempotently mkdirs the env root for fresh dev envs so first-time bootstrap is one line: `eval "$(minds env activate --create <your-user>-dev)" && minds env deploy`. `minds env deploy` and `minds env destroy` no longer take a name argument -- they operate on the currently-activated env and refuse loudly when nothing is activated. `minds env destroy` supports staging (gated by `--yes-i-mean-staging`; stops the deployed Modal apps and removes the env root, leaving operator-managed Vault/Neon/SuperTokens state in place) and hard-refuses production regardless of any flag. `minds env list` globs `~/.minds*/` directly so every env on disk shows up regardless of deploy state.

All deploys flow through `minds env deploy`. The standalone `scripts/deploy_remote_service_connector.sh`, `scripts/deploy_litellm.sh`, and `scripts/push_modal_secrets.py` are removed; their work folds into the unified CLI. Tier deploys (staging / production) require a mandatory `--yes-i-mean-<tier>` flag, push Vault secrets straight to Modal, and run `modal deploy` for both apps -- writing nothing to disk because the committed in-repo `client.toml` is the source of truth for those tiers. Dev env deploys also write `~/.minds-<name>/{client.toml,secrets.toml}` so re-deploys can find their per-env state.

`minds run` (and `propagate_changes`, and every justfile recipe that touches mngr state) refuse without an activated env. No implicit fallback to a hardcoded dev `client.toml`; the dev tier's static `client.toml` is deleted entirely (only `dev/deploy.toml` remains). The packaged Electron build drops `MINDS_BUILD_TIER` in favor of explicit `MINDS_CLIENT_CONFIG_BUNDLE=<path>` + `MINDS_ROOT_NAME_BUNDLE=<minds(-<env-name>)?>`; the runtime exports `MINDS_ROOT_NAME` from the embedded value and passes `--config-file` from the embedded path so a beta or staging build never collides on disk with an installed production build. `just devminds-start` and `forward-{minds,devminds}-system-interface` are gone -- replaced by a single env-agnostic `just minds-start` and `forward-system-interface` that read the activated env from the shell.

`minds env destroy` now actually destroys everything `deploy` created, plus clears the env-specific data accumulated inside operator-managed shared resources (so the next deploy starts from a clean slate). For every env destroy: `mngr destroy` every agent under the env's mngr profile first, then walk the cloud-side teardown, and only `rmdir ~/.minds-<env-name>/` if every cloud step succeeded -- a partial failure leaves the env root in place so re-running picks up where things broke. Dev env destroy deletes the per-env Modal env (cascade-deletes apps/secrets/volumes), Neon DB, and SuperTokens app outright; the new staging tier destroy (gated by `--yes-i-mean-staging`) `modal app stop`s both apps, `modal secret delete`s every per-tier Secret, wipes the SuperTokens app's users via delete+recreate of the same `app_id`, and `DROP SCHEMA public CASCADE`s the Neon DB via psql. Both paths now also enumerate + delete Cloudflare tunnels tagged with `metadata.env=<env-name>` (set by `cf_create_tunnel` at create time when the connector reads the new `MINDS_ENV_NAME` env var) and delete Vultr instances tagged `minds_env=<env-name>` (renamed from the dev-only `minds_dev_env`).

A new per-tier generation id is minted at deploy time, stored at `secrets/minds/<tier>/generation` in Vault, exposed by the deployed connector at `GET /generation`. `minds env activate` fetches the id and compares it against a per-env `last_seen_generation` marker on disk -- on mismatch (i.e. the tier got destroyed + redeployed since the dev last activated) the activation auto-wipes the env's `mngr/` / `auth/` / `logs/` subdirs so the dev's next `mngr list` / `minds run` doesn't surface stale state pointing at the (now-gone) previous deploy.

Also: minds shutdown is cleaner now (terminates the `mngr forward` subprocess before draining the concurrency group, so reader threads no longer time out on every clean exit); the browser auto-open lands directly on the login URL with the one-time code instead of the bare origin; `list_agents`' ABORT-mode failures are now properly attributed to the failing provider so minds' auto-disable-on-auth-error handler actually fires; and `scripts/push_vault_from_file.py` pipes values as JSON on stdin to avoid the vault CLI's `@`-as-file sigil. New docs at `apps/minds/docs/environments.md` and `apps/minds/docs/vault-setup.md` walk through the new operator workflow.

## 2026-05-14

## minds: switch permission management to the latchkey 2.9.0 gateway extensions

Latchkey 2.9.0 ships two new gateway extensions that this branch wires
into the minds desktop client (in coordination with `mngr_latchkey`):

- `permission_requests.mjs` -- per-process pending-permission queue.
  Agents `POST /permission-requests` when they hit a blocked service;
  the desktop client consumes `GET /permission-requests?follow=true`
  to learn about new requests and `DELETE /permission-requests/<id>`
  to clear them once granted or denied.
- `permissions.mjs` -- a `permissions.json` editor that operates on any
  file path inside `LATCHKEY_EXTENSION_PERMISSIONS_ROOT`. Used by the
  desktop client to apply per-host permission grants via
  `POST /permissions/rules?path=<host_file>&rule_key=<scope>`.

### Minds desktop client

- `cli/run.py` now blocks on `_wait_for_gateway_port` (which polls
  `LatchkeyForwardInfo.gateway_port` for a non-None value) before the
  FastAPI app is built, then derives the gateway password and mints
  the admin JWT in-process and constructs a `LatchkeyGatewayClient`
  shared by every code path that talks to the gateway extensions.
- New `PermissionRequestsConsumer` daemon thread streams
  `GET /permission-requests?follow=true` and feeds each pending
  request into the existing `RequestInbox`. The legacy
  `events.jsonl` callback now ignores `LATCHKEY_PERMISSION` lines
  because the extension owns that flow; non-latchkey
  `PERMISSIONS` events still go through the JSONL channel
  unchanged.
- `LatchkeyPermissionGrantHandler` applies grants via the new
  `permissions` extension (`POST /permissions/rules?path=...&rule_key=...`)
  and clears the pending gateway record via `DELETE
  /permission-requests/<id>` on both grant and deny.
- New `gateway_client.py`, `permission_requests_consumer.py`, and
  `testing.py` modules support the above; corresponding unit-test
  files exercise the HTTP wire shape and the streaming/translation
  paths.

### Compatibility

Agents that still post `LATCHKEY_PERMISSION` request events via the
old `events.jsonl` channel will no longer reach the minds inbox.
Migrating agents to the gateway-side `POST /permission-requests`
endpoint is a follow-up.

**minds**: split the services agent from the initial chat agent. The "primary" agent in a minds workspace now runs only the bootstrap and background services (its window-0 command is `sleep infinity && claude`, so claude never actually starts) and is hidden from the agent list in the UI. On first container boot the bootstrap creates a real chat agent named after the host, sends it `/welcome`, and writes `CLAUDE_CONFIG_DIR` to the host env so every subsequent agent (chat, worktree, worker) shares the services agent's Claude config dir (auth, plugins, marketplaces, sessions). Destroying chat agents no longer tears down services, and restarting services no longer kills chat agents. The workspace_server `/api/agents/<id>/destroy` endpoint refuses to destroy `is_primary=true` agents as a server-side guard. Existing pre-change workspaces are not migrated — re-create them.

Minds: the "Name" field on the create-project form now sets the *host* name (validated via mngr's `HostName` regex), not the agent name. The agent is always called `system-services`. The imbue_cloud connector grows a required `host_name` on `/hosts/lease` and `/hosts`. Sister change in `forever-claude-template` (matching branch) drops the now-unused `MINDS_WORKSPACE_NAME` from `[commands.create].pass_env`.

## 2026-05-13

# Latchkey state per-host (minds side)

When minds creates an agent, the Latchkey-related env vars
(`LATCHKEY_GATEWAY`, `LATCHKEY_GATEWAY_PASSWORD`,
`LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE`, `LATCHKEY_DISABLE_COUNTING`)
are now passed to `mngr create` via `--host-env` instead of `--env`, so
every agent that ever runs on the host shares the same gateway URL,
password, JWT, and permissions.

The on-disk permissions metadata moves accordingly: minds now stores
the per-agent `latchkey_permissions.json` under
`<latchkey-dir>/mngr_latchkey/hosts/<host_id>/` instead of
`<latchkey-dir>/mngr_latchkey/agents/<agent_id>/`. After `mngr create`
returns, minds reads the canonical `host_id` from the trailing JSONL
`created` event and points the opaque permissions handle (referenced
by the JWT minted at create time) at the new host-keyed path.

The minds UI's grant flow now resolves the request event's `agent_id`
to its `host_id` via the backend resolver before writing the grant; if
the resolver hasn't seen the agent yet (or only reports the static
`"localhost"` placeholder), the grant POST returns 503 so the UI can
retry instead of silently writing the grant to the wrong file.

## 2026-05-12

### Minds-side cleanups for the mngr-latchkey package extraction

- `apps/minds/imbue/minds/desktop_client/ssh_tunnel.py`: removed the
  now-unused `SSHTunnelManager` and supporting types (`ReverseTunnelInfo`,
  `_TunnelFailureState`, `_ForwardedTunnelHandler`, relay imports,
  reverse-tunnel health-check / backoff constants, and the internal
  `_ssh_connection_*` helpers). Kept `RemoteSSHInfo`, `SSHTunnelError`,
  `open_ssh_client`, and `_create_ssh_client` -- still used by
  `backend_resolver.py`, `forward_cli.py`, and the `MindsRemoteSSHInfo`
  adapter in `cli/run.py`. The matching test files
  (`ssh_tunnel_test.py`, `test_ssh_tunnel_leak.py`) moved to the new
  package along with the manager.
- `cli/run.py` and `desktop_client/agent_creator.py` rewired to import
  the latchkey types and helpers from the plugin and wrap the
  raising plugin entry points (`prepare_agent_latchkey`,
  `finalize_agent_permissions`) in try/except blocks that log a
  warning and continue agent creation -- preserving the prior
  end-to-end behaviour where a misconfigured latchkey installation
  does not abort agent creation, but making the choice explicit at the
  call site rather than buried inside the library.
- Three minds `test_ratchets.py` snapshots tightened
  (`while_true 1->0`, `time_sleep 2->1`, `broad_exception_catch 1->0`)
  to reflect violations that went away with the deleted code.

### Minds: spawn `mngr latchkey forward` as a detached subprocess

`apps/minds/imbue/minds/cli/run.py` no longer constructs
`SSHTunnelManager` / `LatchkeyDiscoveryHandler` /
`LatchkeyDestructionHandler` in-process; it instead calls
`LatchkeyForwardSupervisor.ensure_running()` at startup, which spawns
the canonical `mngr latchkey forward` process detached. Minds does
*not* call `supervisor.stop()` on shutdown -- the supervisor keeps
running across desktop-client restarts and the next minds session
adopts it. This matches how minds already treated the underlying
`latchkey gateway` subprocess.

Side effect: the `_LatchkeyDiscoveryAdapter` class in `cli/run.py` is
gone, plus its supporting `MindsRemoteSSHInfo` / `AgentId` imports.

## 2026-05-09

- Fixed: the `minds run` process no longer pegs a CPU after agents or hosts come and go. Reverse-tunnel bookkeeping in the desktop client's `SSHTunnelManager` (used for Latchkey gateways) is now pruned when an agent is destroyed -- so paramiko transport threads can exit instead of being kept alive by repeated re-establishment attempts -- and the 30s health-check loop applies per-tunnel exponential backoff and drops a tunnel after 10 consecutive failed repair attempts.

- Changed: the desktop client's `SSHTunnelManager` reverse-tunnel health check now retries broken tunnels forever (capped at one attempt per 5 minutes via the existing exponential backoff) instead of giving up after 10 consecutive failures. This matches the user-visible expectation that going offline overnight should still result in working tunnels in the morning.

- Removed `LaunchMode.DEV` from minds. The web create form, `/create`, and
  `/api/create-agent` now offer only `LOCAL`, `LIMA`, `CLOUD`, and
  `IMBUE_CLOUD`; submitting `launch_mode=DEV` returns 400. The DEV-only
  latchkey gateway helper, the `MINDS_ALLOW_HOST_LOOPBACK` env var, and the
  `allow_host_loopback` field on `ForwardSubprocessConfig` are gone (the
  generic `mngr_forward --allow-host-loopback` CLI flag stays for
  non-minds consumers).

Companion changes live in the forever-claude-template repo on the
same-named branch (`mngr/tweak-template`): default `~/.tmux.conf`
provisioning, `--cap-add=SYS_PTRACE` for the docker template, removal of
the unused `events_processor/` project, removal of `[create_templates.dev]`,
and the crystallization Stop hook is disabled.

## 2026-05-08

Removed `apps/minds_workspace_server/` from the monorepo. The workspace server (the FastAPI + dockview UI service that runs inside each agent's container) has been migrated to forever-claude-template, where it now lives at `apps/system_interface/` and ships as the `minds-workspace-server` CLI. Consumers (the minds desktop client and mngr) pick it up at runtime from the consumer's vendored forever-claude-template checkout instead of from this repo. Build-time impact: the release Dockerfile no longer cross-references the workspace server's frontend, and the node/npm install step that existed only to build it has been dropped. The `apps/minds/scripts/propagate_changes` dev-loop script now rsyncs from `/code/apps/system_interface/frontend/` in the running agent. User-facing docs (`apps/minds/docs/overview.md`, `apps/minds/docs/workspace/getting_started.md`) and the historical specs that referenced the old path were updated.

## 2026-05-07

- minds now injects `LATCHKEY_DISABLE_COUNTING=1` into every workspace
  whenever latchkey is wired (alongside `LATCHKEY_GATEWAY`,
  `LATCHKEY_GATEWAY_PASSWORD`, and `LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE`).
  The workspace-side `latchkey` CLI runs in client mode against the
  host-side gateway, so suppressing its daily goatcounter.com ping
  prevents every agent from being counted as a separate active user --
  the single host-side gateway already represents the one real user.

- Bumped the `latchkey` npm dependency to 2.8.0 and switched minds to
  running a single shared `latchkey gateway` subprocess for every agent
  instead of one per agent. The gateway is now password-protected via
  `LATCHKEY_GATEWAY_LISTEN_PASSWORD` (the password is derived
  deterministically from the desktop client's Latchkey encryption key by
  hashing a JWT minted with `latchkey gateway create-jwt`, so it
  survives restarts without being persisted in plaintext).
- Each agent gets its own `latchkey_permissions.json`. At
  agent-creation time minds allocates an opaque
  `~/.minds/latchkey/permissions/<uuid>.json` handle, materializes it
  with empty rules (deny-all baseline), mints a permissions-override
  JWT for that path, and injects all three latchkey env vars
  (`LATCHKEY_GATEWAY`, `LATCHKEY_GATEWAY_PASSWORD`,
  `LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE`) at `mngr create` time.
  After `mngr create` returns the canonical agent id, minds replaces
  the opaque file with a symlink pointing at the canonical
  `~/.minds/agents/<agent_id>/latchkey_permissions.json` location, so
  the existing permission-grant flow continues to write to its
  conventional path while the gateway reads through the symlink. The
  gateway's own default permissions config
  (`~/.minds/latchkey_default_permissions.json`) is materialized empty
  (deny-all) so requests that bypass the JWT mechanism cannot reach
  any service.
- DEV-mode agents (which run in-process on the bare host with no SSH
  reverse tunnel) now go through the same gateway as every other
  launch mode. `AgentCreator` queries the gateway's live host port
  and injects it as `LATCHKEY_GATEWAY=http://127.0.0.1:<dynamic_port>`
  alongside the password and JWT. Previously DEV agents bypassed the
  gateway entirely, which made the full latchkey flow impossible to
  exercise from DEV.
- Old per-agent gateway records left under
  `~/.minds/agents/<id>/latchkey_gateway.json` are cleaned up
  automatically on desktop-client startup. Agents that were created
  with earlier minds versions need to be re-created to pick up the new
  env vars; without them their `latchkey` CLI calls will be rejected by
  the now-password-protected gateway.

## 2026-05-06

`apps/minds/scripts/propagate_changes` now protects `.claude/settings.local.json` from `rsync --delete` when syncing the template into an agent's work_dir.

That file is generated per-agent at create time by mngr's `_configure_agent_hooks` and holds the `UserPromptSubmit` hook that signals `tmux wait-for -S "mngr-submit-..."`. Without it, every `send_message` hangs the 90-second submission-signal timeout while the prompt is actually delivered to Claude (so the UI shows the message and Claude responds normally, but the HTTP `/message` request times out).

Previously the script only protected `runtime/` and `.mngr/`, so iterating with `propagate_changes` reliably reproduced the hang -- and there was no easy way to recover short of recreating the agent.

Fix WebSocket broadcaster queue-full flood and hung-send pin: stuck WS clients are evicted after 50 consecutive queue-full broadcasts, and the broadcaster cancels the wedged handler's asyncio task to free a coroutine blocked in `await websocket.send_text(...)` on a half-dead TCP connection. The previous behaviour pegged a CPU core and filled tmux with `WebSocket client queue full, dropping message` warnings whenever a single client stopped draining its queue.

Adds a spec for backing up the gitignored `runtime/` folder of forever-claude-template (which now also contains `memory/` and `tickets/`) into the same private repo on a separate orphan branch, plus a periodic backup service and `GH_TOKEN`-based auto-push setup.

- minds desktop client: when a discovery error from the connector indicates a revoked SuperTokens session for a specific imbue_cloud account, the matching `[providers.imbue_cloud_<slug>]` block is automatically marked `is_enabled = false` and `mngr observe` is bounced so the dead account stops poisoning subsequent discovery cycles. Signing back in (email/password or OAuth) re-enables the provider. The Manage Accounts page shows a "Signed out" badge + "Sign in again" link for any account whose provider is currently disabled.
- minds desktop client now installs a grandparent-death watcher when the Python backend starts: if Electron crashes (or is otherwise killed without running its on-quit handler), the Python backend self-terminates within ~3 seconds, and the cascade brings down its `mngr observe`/`mngr events`/latchkey children via their own watchers. Previously a crashed Electron left an orphan tree alive across restarts.
- minds: SIGTERM that minds itself sends to `mngr observe` / `mngr event` subprocesses (during shutdown, observe restart, or events-stream sync after an agent leaves the discovery snapshot) no longer surfaces as a "subprocess failed" notification.

- minds: redesigned the "Create a Project" screen.
  - Removed the "Include .env file" checkbox.
  - Added an "AI provider" choice (`imbue_cloud`, `api_key`, `subscription`) that is independent from the compute provider, so any combination is valid as long as `imbue_cloud` is paired with a selected account.
  - Renamed the "Launch mode" dropdown to "Compute provider"; both compute and AI provider default to `imbue_cloud` when an account is selected.
  - Selecting `api_key` reveals a required Anthropic API key field; `subscription` injects no Anthropic credentials so the user can sign in interactively after the workspace starts.
  - Selecting `imbue_cloud` for either field with no account is rejected by both the form (with a warning) and the server (with a 400).
  - Added an optional `GH_TOKEN` field under Advanced settings that is forwarded to the agent host (or the agent in DEV mode).

Cleanup pass after splitting functionality out of minds into the `mngr_imbue_cloud` and `mngr_forward` plugins.

- The "Share" button in a workspace now opens a static informational modal that points the user at the desktop app, rather than writing a sharing-request event back to minds. Direct sharing editing from the desktop client (workspace settings page) is unchanged. Permissions / latchkey request flows are unchanged.
- Minds no longer persists `imbue_cloud` account identity (email, display_name) to disk. Only the workspace<->account association map lives in `~/.minds/workspace_associations.json`; identity is sourced on demand from the new `mngr imbue_cloud auth list` command and cached in memory.
- Destroyed agents now disappear from the projects index automatically without requiring the user to click into the destroying detail page first.

# minds run

A new `minds run` command rewires the minds desktop client to spawn
`mngr forward` as a subprocess instead of running the same forwarding
logic in-process:

```bash
minds run --port 8420 --mngr-forward-port 8421
```

- Spawns `mngr forward --service system_interface --preauth-cookie ...`
  and consumes its envelope JSONL stream on stdout.
- Serves the slimmed minds bare-origin UI on `--port` (default 8420);
  agent subdomains are served by the spawned `mngr forward` on
  `--mngr-forward-port` (default 8421).
- Emits a `mngr_forward_started` JSONL event on stdout carrying the
  preauth cookie value so the Electron shell can pre-set
  `mngr_forward_session=<value>` on `localhost:<mngr-forward-port>`
  before the first agent-subdomain navigation.
- Sends `SIGHUP` to the plugin's PID after a freshly-written
  `[providers.imbue_cloud_<slug>]` block in `settings.toml` so the new
  provider becomes visible without restarting the plugin.

The legacy `minds forward` command and its in-process forwarding /
auth / subdomain code are intentionally unchanged in this branch and
keep working. A follow-up branch will delete the now-duplicated
in-process paths.

QA pass for the merged forwarding refactor on top of `josh/imbue_cloud_ready`:

- Resolved a `test_ratchets.py` merge conflict in `mngr_imbue_cloud` (kept the standard layout, set the `bare_print` snapshot to 1 to match the surviving `sys.stderr.write` in `cli/admin.py`).
- Pruned the dead `tunnel_token_store` re-injection path from `LocalAgentDiscoveryHandler` (the parallel `mngr/imbue-cloud` branch dropped that cache; the agent's container persists the token now and rebuilds re-fire the post-create injection).
- Passed `concurrency_group=` to `LatchkeyDiscoveryHandler` in the new `minds run` entry point.
- Switched `apps/minds/electron/backend.js` from spawning `minds forward` to `minds run` so QA exercises the `mngr_forward` plugin subprocess + `EnvelopeStreamConsumer` path.
- Ported `start_grandparent_death_watcher` (Electron-exit detection) and `_ImbueCloudAuthErrorDisabler` (auto-disable an imbue_cloud account whose session has been revoked) from the legacy `desktop_client/runner.py` over to the new `cli/run.py` path. Added an `add_on_provider_error_callback` API on `EnvelopeStreamConsumer` so the disabler has somewhere to register.
- Phase 2 cleanup of the `mngr_forward` split:
  - Deleted `desktop_client/runner.py` and `cli/forward.py` + `cli/forward_test.py` (the legacy `minds forward` command).
  - Deleted `MngrStreamManager` from `desktop_client/backend_resolver.py` (replaced by `EnvelopeStreamConsumer` in `forward_cli.py`) and dropped the corresponding test block from `backend_resolver_test.py`.
  - Slimmed `desktop_client/cookie_manager.py` to the minds bare-origin session helpers; the per-subdomain auth-token helpers live in the plugin's `cookie.py`.
  - Slimmed `desktop_client/app.py`: deleted the host-header subdomain-forwarding middleware and many supporting helpers; `create_desktop_client(...)` no longer takes `tunnel_manager`, `latchkey`, or `stream_manager`; it gains `mngr_forward_port` + `mngr_forward_preauth_cookie` so server-to-server refresh broadcasts route through the plugin.
  - Rewired `_dispatch_refresh_broadcast` to POST through the plugin's per-agent subdomain (`<agent>.localhost:<plugin_port>/api/refresh-service/<svc>/broadcast`) with the preauth cookie, instead of opening its own SSH tunnel.
  - `supertokens_routes._bounce_mngr_observe` → `_bounce_forward_observe`: sends `SIGHUP` via `EnvelopeStreamConsumer.bounce_observe()`. Dropped the legacy `MngrStreamManager` fallback.
  - Templates and static JS now point `/goto/<agent>/` links at the plugin's port via a `mngr_forward_origin` Jinja variable / `data-mngr-forward-origin` attribute.
  - Electron's `backend.js` exposes a new `onMngrForwardStarted` callback; `main.js` consumes the `mngr_forward_started` event from `minds run` stdout and pre-sets the `mngr_forward_session=<preauth>` cookie on `localhost:<plugin_port>` (default + content session) before any agent-subdomain navigation.
  - Updated user-facing references to `minds forward` → `minds run` in `apps/minds/README.md` and `apps/minds/docs/{design,desktop-app,overview,workspace/getting_started,workspace/glossary}.md`.

## 2026-05-05

- Fixed: closing the last tab in a minds workspace no longer leaves a blank screen with no recovery path. The primary agent's chat tab is automatically reopened when the dockview becomes empty (whether by closing all tabs at runtime or restoring an empty saved layout).
