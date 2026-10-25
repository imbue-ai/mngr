# Unabridged Changelog - mngr_latchkey

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_latchkey/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-24

Bump Latchkey to 3.19.4.

Documented and suppressed Bandit's B701 (`jinja2_autoescape_false`) finding on the remote package's template environment. The templates render shell scripts and system config files, never HTML, and HTML autoescaping would corrupt the `shquote`-quoted values in them, so autoescaping stays off; the reason is now recorded at the site and the finding is marked `# nosec B701`.

## 2026-10-23

A remote workspace's gateway now answers `GET /permissions/self` itself, from the permissions file it enforces, instead of forwarding the read to the user's desktop. Reading what an agent may do no longer fails with `Desktop <device> (<hostname>) is unreachable` while the user's computer is asleep, offline, or has Imbue Studio closed, and the answer is the policy the machine applies to the agent's requests (every desktop edit reaches the machine before the desktop reports it). It leaves out the per-agent Studio API rule, which only the desktop applies, to forwarded `/minds-api-proxy` requests. A request that names a desktop with `X-Latchkey-Device`, and every other `/permissions`, `/permission-requests` and `/minds-api-proxy` request, is still forwarded.

## 2026-10-21

Bump Latchkey to 3.19.3.

An SSH authentication timeout while wiring a remote workspace's latchkey gateway (VPS gateway provisioning or device announcement) is now treated as a transient failure, as the transient-failure streak reporting intended: it is logged at info, retried on the next discovery cycle, and reported as an error only once it persists for `TRANSIENT_FAILURE_REPORT_THRESHOLD` consecutive cycles. The outer host reports every paramiko authentication failure, timeouts included, as a `HostAuthenticationError`, which `is_transient_remote_wiring_error` treated as a rejected key, so every sshd that was slow to answer authentication produced an error-level Sentry event. A key sshd actually rejects is still reported at once.

## 2026-10-20

When a wiring step's transient-failure streak reaches the report threshold, the error now also says when the streak started, which SSH info fields changed during it (field names only), and since when this process has seen the host in its reported state. That separates a host that moved or restarted under the streak from one that kept refusing the same connection.

## 2026-10-19

Bump Latchkey to 3.19.0.

`Latchkey.import_chrome_browser_state()` now returns a `ChromeImportOutcome` (`is_success`, `detail`, `error_code`) instead of a `(bool, str)` tuple. When latchkey (>= 3.19) reports the failure as a coded one -- a single JSON line `{"errorCode": ..., "errorMessage": ...}` on stderr, which the crash-dump summarizer used to discard as noise -- the outcome carries latchkey's sentence as `detail` and, for a code this package knows, the code as a `LatchkeyErrorCode`. The one code so far is `CHROME_PROFILE_NOT_READABLE`: the Chrome profile exists but could not be read, which on macOS 27 and later means the application running latchkey lacks Full Disk Access. `parse_coded_latchkey_failure()` does the parsing and is usable on any latchkey CLI output.

`FakeLatchkey.configure(import_chrome_result=...)` takes a `ChromeImportOutcome` accordingly.

- The `permission-requests` gateway extension's comments no longer enumerate the workspace verbs (a list that had drifted: it named a retired `sharing` verb and lacked `backups-manage`); they point at the catalog, `workspace_permissions.json`, instead. No behaviour change.

## 2026-10-18

- `mngr latchkey forward` is no longer spawned detached by the Imbue Studio desktop client: it is a coupled child, stopped on the app's shutdown, and the new `--parent-pid <pid>` flag makes it exit on its own when that process is gone, so the gateway, its `mngr observe` child, and the reverse tunnels never outlive the app that started them. `LatchkeyForwardSupervisor` gains a `parent_pid` field and `stop()` is now part of the embedder's teardown.

- `spawn_detached_mngr_latchkey_forward` and `spawn_detached_latchkey_ensure_browser` are renamed `spawn_mngr_latchkey_forward` and `spawn_latchkey_ensure_browser`; neither starts a new session any more.

- The `mngr latchkey forward` help text no longer says `Latchkey.initialize()` adopts or discards a pre-existing detached gateway record; it version-checks the binary and writes the bundled service registrations into latchkey's `config.json`, and nothing adopts a gateway.

## 2026-10-17

Carries the per-provider machine sizing branch (`mngr/per-provider-sizing`, PR #1750) merged into the account-owned remote providers branch ahead of its own landing on main; the entry `mngr-per-provider-sizing.md` in this directory describes the change. Nothing beyond that merge touches this project.

## 2026-10-14

Bump Latchkey to 3.18.3.

The `minds-api-proxy-per-agent-unauthorized` gate is no longer part of the policy a remote workspace's machine enforces. The policy that crosses the wire to or from a machine is now the host's policy minus that one rule, projected as an update's document is built and restored as a read's answer is parsed (`remote/_machine_policy.py`), so every push and every adopt gets it without the callers changing. The scope schema, and the registered-agent list it carries, still travel with the policy.

The machine never served a per-agent `/minds-api-proxy/api/v1/agents/<id>/...` route itself: it forwards each one to a desktop whose gateway runs the same gate against the same per-host policy. The machine's copy therefore rejected nothing the desktop would not, except when no desktop was reachable, where an agent whose id no desktop had registered yet was answered with a 403 "Request not permitted by the user" instead of the forwarder's 502. An agent created while the desktop is away (for example from a phone while the laptop sleeps) now sees the desktop as unreachable rather than as having refused it. The online race between an agent's first call and its discovery-driven registration is unchanged.

`store.dump_permissions_text` is the one serializer every permissions file this plugin writes goes through; `save_permissions` and the provisioning seed now share it.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-12

Bump Latchkey to 3.18.1.

Bring Slack permission definitions up-to-date.

The `minds-workspaces-recover` verb of the cross-workspace API now also admits `POST /api/v1/workspaces/<id>/machine-size`, the route that resizes a workspace's machine and restarts it, so an agent granted recovery on a workspace can resize it; before, the gateway refused that route whatever the agent was granted.

## 2026-10-09

Bump Latchkey to 3.18.0.

Bump Latchkey to 3.17.0.

`Latchkey.auth_browser_login` now runs `latchkey auth browser <service> --strict`, so a browser sign-in fails with a message naming the missing permissions, instead of storing a partial credential, when the user grants fewer permissions than the service asked for (for example by leaving some Google consent checkboxes unticked). Only services that can tell what was granted enforce it; for the others the flag is a no-op.

## 2026-10-08

- Every answer a remote host's machine composes for `POST /permission-requests` rather than relays from the one desktop it reached (the responses side by side for several desktops, the 503 for a request that reached no desktop, the 502 for one unreachable desktop) now opens with the `request_id` the machine assigned, the `request_type` (the body's `type`, under the name the desktops file it as), the `rationale` and the `payload` (each `null` when the body lacks it), ahead of its other fields, so a parser of the agent's output learns what was filed and under which id whatever the desktops said. A single desktop's own response is still relayed untouched.

- A remote host's machine now takes part in `POST /permission-requests`: its forwarding extension assigns the request its id before forwarding (sent in the body as `request_id`; a body carrying one is a 400), so every desktop the `X-Latchkey-Device` header names files the same request under the same id, and keeps a record of what the agent sent under `~/.latchkey/filed_permission_requests/v1/<request_id>.json` (`imbue.mngr_latchkey.filed_permission_requests.FiledPermissionRequest`: the body, the desktops it was for, and when; a reader ignores fields it does not know, so a newer package may add to the record without an older desktop's sync failing). The record is kept when some desktop accepted the request or none could be reached to judge it, and dropped when every desktop that answered refused it. A request that reaches no desktop is still a 503, now saying the request was kept for the desktops to pick up. A `DELETE /permission-requests/<id>` forwarded through the machine drops its record too.

- The desktop's `permission-requests` extension accepts an optional filename-safe `request_id` in a POST body (409 when one is already pending under it; `effect` is still the one field a caller may not supply), and its `?follow=true` stream now carries `{"event": "deleted", "request_id": "..."}` whenever a request stops being pending through it (approved or deleted), so a consumer learns of a request withdrawn by another desktop.

- The agent baseline grants `DELETE /permission-requests/<id>` (`latchkey-self-delete-permission-request`): a machine withdraws a request one desktop answered from the others through the hop credentials each desktop announced, which name the host's own policy, and an agent can withdraw a request it filed. An existing host's policy gains the grant the way it gains every new baseline permission: when the app next registers the host's agents, which it does for every discovered agent on each start, and pushes the reconciled policy to the host's machine.

- The `mngr-latchkey` package gains two commands: `list-requests` answers with the requests the machine keeps as one JSON array, and `forget-request` drops the request the document names and sends the DELETE for every desktop through the machine's own gateway, best-effort (`MachineCredentials.list_filed_permission_requests` and `forget_permission_request`, over `remote._machine.read_filed_permission_requests` and `forget_permission_request`).

Removed the self-setup fallback for Google OAuth services (Gmail, Calendar, Drive, Docs, Sheets, Slides, People, Analytics). A Google sign-in now uses only the OAuth client Imbue Studio provides: when that sign-in does not succeed (for example because the user closes the consent screen), `Latchkey.auth_browser` and `Latchkey.add_account` return the failure instead of running `latchkey auth browser-prepare` (which created an ad-hoc Google Cloud project on the fly) and opening a second browser window. The fallback existed only because the Imbue Studio Google project was unverified and capped at 100 test users; the project is now verified. Non-Google services still get the `browser-prepare` step when latchkey asks for it.

Comment and docstring prose that referred to the desktop app as "Minds" now
says "Imbue Studio": the gateway's Google OAuth notes (the OAuth client the app
provides and its consent screen) and the tests covering them, the discovery
module's tunnel notes, the WebDAV and permission-request extension docs, the
default-permissions docstring, the machine-store re-export note, the
cross-workspace verb metadata, the services-catalog generator's docstring, and
the account-scope prose in both `account_scopes.py` and `README.md`.

No behavior change. `minds-api-proxy`, `minds-workspaces` and
`MINDS_GOOGLE_OAUTH_SERVICES` are identifiers and keep their `minds` spelling
for good, per `apps/minds/style_guide.md`. The one name still to move is the
"Minds API" product name, which is an agent-facing wire path and needs a compat
alias, so it changes separately.

Review follow-up to the Imbue Studio prose sweep.

The sweep had changed the OAuth and WebDAV prose in `core.py`, `discovery.py`
and `permission_requests.mjs` but not the tests and README that repeat it, so
one project named the product both ways. `README.md`'s account-scope section,
`core_test.py`'s OAuth-client prose, `services_catalog_test.py`'s scope comment
and `custom_services_test.py`'s catalog comment now say Imbue Studio too.

Ten hyphenated compounds are reworded. "Imbue Studio-provided client" parses
as "Imbue [Studio-provided]" now that the name is two words, so these follow
the form the product docs already use: "the client Imbue Studio provides", "the
redirect URI Imbue Studio hosts", "the endpoints Imbue Studio owns".

This project's sweep entry claimed `minds-api-proxy`, `minds-workspaces` and
`MINDS_GOOGLE_OAUTH_SERVICES` were "being handled separately". They are
identifiers and keep their `minds` spelling for good, per
`apps/minds/style_guide.md`; the entry now says so, and its list of what the
sweep touched is filled out.

## 2026-10-07

- File-sharing grants are scoped to one desktop. The gateway's `permission-requests` extension mints a `file-sharing` grant for the desktop it runs on (`LATCHKEY_EXTENSION_LOCAL_DEVICE_ID`): the permission is named `minds-file-server-<access>-<device id>:<path>` and matches `/minds-api-proxy/api/v1/files/<device id><path>`. A desktop therefore cannot grant access to another desktop's files, and a gateway without a device id refuses file-sharing requests with HTTP 503. The request payload is unchanged; a workspace picks the desktop by sending the request there with `X-Latchkey-Device`.

- The first permissions migration (format version 1) gives each existing file-sharing grant a twin naming the desktop that migrates the policy. The existing grant is left in place, so a workspace built against the device-less URL keeps its access. `PermissionsMigration.apply` now also takes a `PermissionsMigrationContext` carrying that desktop's device id, and `migrate_permissions`, `migrate_permissions_and_push`, `MachineCredentials.refresh` and `provision_remote_gateway` take it too.

- Approving a `file-sharing` request computes its grant at approval rather than applying the effect stored when it was filed, so a request filed by a build from before this change and approved after it also yields a grant naming the desktop.

- The header that names the desktop a request is for is renamed from `X-Latchkey-Desktop` to `X-Latchkey-Device` (`imbue.mngr_latchkey.devices.DEVICE_HEADER`, formerly `DESKTOP_HEADER`). The gateway's forwarding extension no longer reads the old name: a request that still sends it is handled like one without the header and goes to the most recently announced desktop.

- New `imbue.mngr_latchkey.file_sharing` module reads and writes file-sharing permission names, those from before grants named a desktop included, and now owns `FileSharingAccess`.

- Desktop egress is now a route per service instead of a boolean. `proxyRules.json` gives each latchkey service an ordered list of hops for the machine's curl router to try: each hop is a desktop's device id, and the last one may be `self` for the machine itself. `["host-a"]` sends a service's requests through one desktop, `["host-a", "host-b"]` tries one and then another, and `["host-a", "self"]` falls back to the machine. A service the file does not name is at `["self"]`, which is never written out.

- Every desktop on a route is named by its device id, and the device-gated rule that makes a desktop gateway accept a service's requests is still for exactly one desktop (`desktop-egress:<device id>:<scope>`). A route needs one such rule per scope of its service for each desktop it names; whoever writes a route writes those rules with it.

- `imbue.mngr_latchkey.desktop_egress` has new public names: `DesktopEgressRoute`, `build_desktop_egress_route`, `SELF_HOP`, `SELF_ONLY_DESKTOP_EGRESS_ROUTE`, `desktop_egress_forwarders` (the desktops a route names), `DesktopEgressMode` and `desktop_egress_mode_for_route` (off, on through this computer alone, or custom), `DesktopEgressRules`, `desktop_egress_route_for_service` and `desktop_egress_route_from_grants`.

- `parse_desktop_egress_rules` now returns a `DesktopEgressRules` (the routes the file holds, plus the services an older file turned on with a bare `true`) and refuses a rule that is not a route. `serialize_desktop_egress_rules` takes the route of each service. `build_desktop_egress_rules` and `is_service_routed` are gone.

- A rules file written before routes holds `true` per turned-on service and names no desktops. Its reader does not guess a route: `desktop_egress_route_from_grants` derives one from the host's permissions file, as the desktops that hold a desktop egress rule for every scope of the service, in file order, or `["self"]` when there is none. Such a file is rewritten as routes by the first route change made for its host.

- A `predefined` permission request may carry `"proxy": true`: the agent asks that the service's requests also be sent out through the user's computer, the one that approves the request. The `permission-requests` extension accepts only a boolean (anything else is an HTTP 400), stores `true` on the request's payload (`false`, `null` and an absent `proxy` store nothing), and never acts on it: it is not a permission and does not enter the `effect`.

- The curl router pinned for the machine moves from latchkey-curl-shims v0.5.0 to v0.6.0, the first release whose router reads routes; the machine picks it up on its next provisioning pass. The router sends a request to the first hop of the service's route that is satisfied: a desktop whose device record was touched in the last three minutes, or `self`. The request is sent once, so a failure at that desktop is a failed request and not a reason to try the next hop. A `true` left in a rules file written before routes still goes to the most recently announced desktop, now only one whose record is that fresh.

- A permission grant that also changes a route reaches the machine in one remote command: `MachineCredentials.connect_service_with_permissions_and_desktop_egress_rules` pushes the granted account, the permissions snapshot and the desktop egress rules snapshot together. `push_credentials_with_permissions` takes the rules snapshot as a new required `desktop_egress_rules_json` argument (`None` leaves the machine's rules as they are).

## 2026-10-05

Fixed `mngr latchkey forward` running out of usable file descriptors after a few hours, which made every desktop-owned route a remote workspace calls (notifications, error reports, permissions, permission requests) fail with `Desktop <device> (<hostname>) is unreachable: socket hang up`.

Each discovery cycle wires every remote agent on a fresh thread, and that thread announces this computer to the workspace's machine with a host command. pyinfra reads command output with gevent, so each such thread created a gevent Hub whose pipe pair was never closed: two descriptors leaked per announcement, every 30 seconds per remote host. Once descriptor numbers passed 1024, the reverse-tunnel relay's `select()` raised on every request. The discovery-setup, provisioning and SIGHUP-watcher threads now start through `start_mngr_thread`, which destroys the thread's Hub when it finishes.

## 2026-10-02

Each of the user's desktops now keeps a reverse SSH tunnel of its own to every remote workspace's machine, so two desktops running at once no longer contend for one tunnel. A desktop asks the machine's sshd for a port of its own and announces itself to the machine on every discovery cycle (`mngr-latchkey announce-device`, a record under the RAM-backed `/run/mngr-latchkey/devices/` directory carrying its device id, hostname, tunnel port and the secrets the machine's gateway presents on the hop back). The package also ships an sshd drop-in (`ClientAliveInterval 30`, `ClientAliveCountMax 3`) so the machine drops the tunnel of a desktop that went quiet within 90 seconds, and a request for that desktop fails at once instead of hanging. `mngr latchkey forward` takes the id it announces itself under from `--device-id` (or `MNGR_LATCHKEY_DEVICE_ID`), falling back to the mngr local host id.

The VPS gateway's forwarding extension now reads those records per request instead of a single desktop's secret pair written by provisioning. It gained a `GET /devices` route listing every desktop the gateway knows with when each was last heard from and how often a connected one checks in, and honors an `X-Latchkey-Device` header on `/permissions`, `/permission-requests` and `/minds-api-proxy` requests: one device id targets that desktop, `*` or a comma-separated list (unknown device ids ignored) sends the request to each named desktop and answers with the responses side by side -- or, when that comes down to a single desktop, with that desktop's response as is, or with a 503 when no named desktop is known; the aggregated answer alone carries an `X-Latchkey-Multiple-Desktops-Matched: true` response header -- and a request without the header goes to the most recently announced desktop, so existing workspaces keep working unchanged. The desktop gateway still serves those routes itself, ignoring the header; a new `device_list.mjs` extension answers `GET /devices` there with the desktop itself as the one device, so a workspace uses the same routes and header whether it is local or remote. The agent baseline grants `GET /devices`.

The fixed-port desktop-to-VPS tunnel and the desktop secret pair provisioning used to write for the curl router are gone: desktop egress reads the same device records and goes to the most recently announced desktop. The package now pins `latchkey-curl-shims` v0.5.0, the release whose router reads the records; the VPS picks it up on its next provisioning pass.

Added a per-host permissions migration mechanism (`imbue.mngr_latchkey.migrations`) for a host's `latchkey_permissions.json`.

- Each host's directory now carries a `permissions-format-version` stamp, one integer naming the format its policy is written in; a directory without one is at version 0. A host with a machine of its own keeps the same stamp beside its policy in the machine's `~/.latchkey`, where it is the source of truth: `read-state` answers with it, `apply-state` installs it after the policy, every push of a policy carries the stamp of the copy it was taken from, and every read adopts the machine's stamp with the policy. It is distinct from upstream's `data-format-version`, which stamps the credential store.

- A migration is a `PermissionsMigration` with a `version` (consecutive from 1) and an `apply(permissions)` that takes the parsed policy (`LatchkeyPermissionsConfig`) as the version below wrote it and returns it as its own version writes it; the runner reads, writes and stamps. The build's migrations are listed in `migrations/runner.py` (`PERMISSIONS_MIGRATIONS`, currently none); a policy this build creates is stamped with the version the last one ends in, so a fresh file is never migrated.

- `migrate_permissions` applies the migrations a host's stamp says it has not had yet, in order, re-stamping after each. For a host with a machine of its own it runs right after the machine's policy is adopted, by `MachineCredentials.refresh` and by the provisioning pass, and the migrated policy is handed back to the machine in one command (`migrate_permissions_and_push`). For a host without one it runs in place when `mngr latchkey forward` starts, before the gateway reads the file.

- A policy stamped newer than a build knows is refused rather than read: `MachineCredentials.refresh` raises `PermissionsFormatNewerError`, while the provisioning pass and the forward's startup sweep log a warning and leave the policy as it is.

- `push_permissions_snapshot`, `push_permissions_and_desktop_egress_rules` and `push_credentials_with_permissions` take the format version the snapshot is written in; `FetchedMachineState` and `RemoteMachineState` report the machine's; `link_opaque_permissions_to_host` reports whether it created the host's file.

## 2026-10-01

Bump Latchkey to 3.16.2.

`Latchkey.import_chrome_browser_state()` runs `latchkey auth import-chrome` (latchkey >= 3.16), which imports the cookies of the user's default Google Chrome profile, and the localStorage of the login pages of the services latchkey knows, into the directory's encrypted browser state. The browser state is one of the files every machine store shares with the desktop's, so one import serves every machine's sign-ins. The call is bounded by a five-minute timeout and returns `(True, "")` on a clean exit or `(False, detail)` with latchkey's reason; nothing depends on the wording of a successful run's output.

`FakeLatchkey` gains a non-spawning `import_chrome_browser_state` double, configured through `configure(import_chrome_result=...)` and counted by `import_chrome_call_count`.

On a fresh Imbue Studio install, a user's first workspace now gets a working latchkey gateway without an app restart.

The `mngr latchkey forward` supervisor starts before the user signs in. Signing in writes the user's imbue_cloud provider to the mngr settings, together with a disable section for the `recursive` plugin, which the packaged app does not ship, and then sends the supervisor SIGHUP to reload its providers. That reload used to fail with "Cannot disable plugin 'recursive' because it is not registered", so the supervisor kept its empty startup provider set. It could not resolve a route to the new workspace and never wired its gateway, and every `latchkey` call from the workspace failed with "Failed to reach latchkey gateway" until the app was restarted. The reload now picks up the new provider.

## 2026-09-30

Removed the 32 comment lines that had accumulated inside `libs/mngr_latchkey/imbue/mngr_latchkey/test_ratchets.py` explaining why individual ratchet counts had been raised.

A ratchet file records counts, not history. The reason a count moved now goes in the commit message and the changelog entry, the reason a particular code site is allowed goes in that module's docstring, and the set of exempt files goes in the `excluded_patterns=(...)` argument -- all places that stay correct as the code changes, which the comments did not.

Bump Latchkey to 3.16.1.

Every Latchkey subprocess this package spawns (the desktop and VPS gateways, `latchkey auth ...` sign-in flows, `ensure-browser`, and the rest) now runs with `LATCHKEY_APP_NAME_PREFIX=Imbue`, so the API keys, OAuth clients and developer apps Latchkey's browser automation creates inside a service are named after Imbue rather than carrying upstream's default "Latchkey" prefix. A `LATCHKEY_APP_NAME_PREFIX` already set in the environment still wins, matching how the encryption key override works. The VPS package's version changes with its rendered scripts, so a provisioned machine picks the prefix up on its next provisioning pass.

## 2026-09-29

`test_bounce_sighups_forward_once_gateway_port_is_stamped` is marked `@pytest.mark.flaky`. On CI it once failed with "orphan forward never took the ownership lock" and passed on retry.

Bump Latchkey to 3.16.0.

The ownership lock is now the only record a `mngr latchkey forward` keeps.

`latchkey_forward.json` and everything that read it are gone. It existed to name a forward from a build that predates the lock -- such a forward holds none, so nothing lock-based can see it -- and the first launch of the release that introduced the lock replaced every one of them. With no pre-lock forward left to find, the record has nothing to say.

Removed with it: the module that recognised such a forward by its process title, the check that refused to start beside one, and the `LatchkeyForwardInfo` type. Nothing in the package reads a process title any more, for any purpose.

This must not ship before the release that introduces the ownership lock has reached users. Until then `latchkey_forward.json` is the only thing that can find a forward predating it.

Latchkey's README, CLI help and gateway logging now call the desktop app Imbue Studio.

A Google-account sign-in failure logs "Adding a Google account for <service> via the Imbue Studio client did not succeed". The gateway route name `minds-api-proxy`, the `minds-api` permission names, and the "Minds API" surface name are unchanged.

## 2026-09-25

A `custom-service` permission request may name the header a pasted token is sent as. Before, a custom service without a browser sign-in could only take `Authorization: Bearer <token>`, so an API keyed by `X-Api-Key` or any other header was out of reach even though `latchkey auth set` stores arbitrary headers. The request payload gains an optional `header`, a header line with `{token}` where the value goes, validated by the same rule as `token-capture`'s `header` plus what the gateway must never let a request set (`Host`, `X-Latchkey-*`), and refused alongside a `login` flow, which carries its own credential shape. The gateway extension and `custom_services.validate_credential_header` share one accept/reject corpus. The header is stored on the service's `registeredServices` entry under `mindsCredentialHeader`, read back by `registration_credential_header`, and `credential_commands.fallback_set_credentials_example` now takes the header. `custom_services.custom_service_credential_header` reads it off a `registeredServices` block by service name, and `credential_commands.set_credentials_example_for` is the one rule every credential form goes through -- the custom-service dialog's, the ordinary permission dialog's, and the Permissions tab's -- so a registration's own header beats the bearer command latchkey reports for every generic registered service, and a token typed into any of them is stored under the header the service actually takes. The payload also gains an optional `credential_instructions`: the agent's plain-text note, at most 500 characters, on where the user finds the credential, for the dialog to show beside the input; it is refused alongside `login` like `header`, and `custom_services.validate_credential_instructions` mirrors the gateway's check, counting characters the same way.

Notion MCP sign-ins now return through the Minds-hosted OAuth callback page instead of latchkey's loopback callback.

Before any `notion-mcp` browser sign-in that registers a new OAuth client (a first sign-in, or "Add account"), `Latchkey.auth_browser` runs `latchkey auth prepare notion-mcp '{"redirectUri": "https://imbue-ai.github.io/oauth-callback/"}'` (latchkey >= 3.15) so the dynamically registered client is bound to that page, which forwards the authorization result to the loopback port latchkey encodes in `state`. A failed pin fails the sign-in with latchkey's own reason rather than falling back to a loopback sign-in. Re-signing in to a stored account (`--account`) is unchanged: latchkey reuses that account's own client and redirect URI.

The redirect-URI services live in `MINDS_OAUTH_REDIRECT_URI_BY_SERVICE` next to the Minds Google OAuth client preference, and `Latchkey.auth_prepare_redirect_uri` is the new counterpart of `auth_prepare`.

The gateway's `permission-requests` extension accepts an optional `sync` object on `file-sharing` request payloads (`{"sync": {"conflict": "NEWER"|"THIS_COMPUTER"|"WORKSPACE"}}`, `conflict` defaulting to `NEWER`), so an agent can ask for a synchronized copy of the folder along with access to it. The extension validates and stores the object and never acts on it: a sync is not a permission, so it does not enter the request's `effect`, and the grant written on approval is the same with or without it.

## 2026-09-24

A remote host's VPS gateway is now provisioned as a Debian package rather than by a sequence of ad-hoc SSH operations. `imbue.mngr_latchkey.remote.package` builds `mngr-latchkey_<version>_all.deb` in pure Python from the `remote/debian/tree/` source tree (jinja templates rendered from the Python constants, plus the bundled desktop-gateway proxy extension), reproducibly and versioned by a hash of its inputs. The package carries the two supervisord programs, their wrapper scripts, the extension, and a `mngr-latchkey` command with two subcommands: `read-state`, which assembles everything the machine holds, and `apply-state`, which makes the machine match a document handed to it on stdin. Its `postinst` installs the pinned latchkey CLI and the curl shims, scrubs what the ad-hoc provisioning left on a machine it set up (its gateway wrapper and staged extension copy), and registers the programs. The teardown of a pre-supervisord (nohup + PID-file) gateway and tunnel that the ad-hoc provisioning still carried is gone: no such machine remains.

The latchkey half of `provision_remote_gateway` (the owner-exec daemon's provisioning rides alongside as before) is now five remote operations however much it has to set right: the docker bridge address probe (shared with the owner-exec daemon), upload the package, run the bootstrap that installs it (and the apt dependencies and Node.js, which cannot be installed from inside dpkg), one `read-state` (which also reports how the agent's container was created, for the reverse-tunnel decision), one `apply-state`. It previously issued around thirty. The machine's own key and listen password are still adopted from a running machine, the desktop-owned pair still replaced, and the policy still seeded or adopted; the seed is now part of the same apply, so `sync_permissions` and the separate `$HOME` resolution (`RemoteLatchkeyDirectory`) are gone. `provision_remote_gateway` takes the `RemotePackageLayout` the package is built for (`DEFAULT_REMOTE_PACKAGE_LAYOUT` on a real machine).

Every credential exchange (`MachineCredentials.refresh`, `connect_service`, `connect_service_with_permissions`, `set_permissions`, `disconnect_account`) is one `read-state` or `apply-state` call too, through `remote._machine`'s `RemoteStateRequest` / `RemoteStateUpdate`. A machine that lost its key to a reboot is handed it back inside the same document, so the second round trip (and the `KEY_MISSING` outcome) that case used to cost is gone. A `read-state` or `apply-state` that succeeds but takes longer than 15 seconds is logged as a warning naming the VPS, so a degrading machine is visible before its commands start hitting the 60-second timeout. Whatever a `read-state` or `apply-state` that succeeded printed on stderr (a warning from a `latchkey` invocation in the middle of it) is logged at debug level. A push that brings credential material carries the recorded key and the machine refuses it if it runs under another one; a sign-out still clears under whatever key the machine runs.

The nftables policy that keeps the gateway's and the owner-exec daemon's ports on the docker bridge (and the docker bridge on those two ports), and the systemd oneshot that re-loads it at boot, ship in the package too (`etc/nftables.d/mngr-bridge-services.nft`, `etc/systemd/system/mngr-bridge-services-firewall.service`): `nftables` is one of the package's Debian dependencies, and its `postinst` loads the policy before the supervisord programs are registered. The package is installed before the owner-exec daemon is provisioned, so neither service ever listens unfenced. `ensure_bridge_services_firewalled`, `build_bridge_services_nftables_policy` and `DockerBridgeFirewallError` are gone from `docker_bridge.py`, which keeps the policy's names (`BRIDGE_SERVICES_NFT_TABLE`, `BRIDGE_SERVICES_NFT_POLICY_FILENAME`, `BRIDGE_SERVICES_FIREWALL_UNIT_NAME`, `NFT_BINARY_PATH`) beside the address resolution. Purging the package unloads the table.

The gateway's and the tunnel's supervisord logs move from `~/.latchkey/{gateway,tunnel}.log` to `/var/log/mngr-latchkey/` (`REMOTE_LOG_DIR`). `LATCHKEY_VERSION`, `CURL_SHIMS_VERSION`, `CURL_SHIMS_SHA256_BY_TRIPLE`, `OUTER_PORT`, `DESKTOP_GATEWAY_VPS_PORT` and the log filenames are defined in `remote.package` and re-exported from `remote.provisioning`. The plugin gains a `jinja2` dependency.

Tests of the remote modules now run the package's real scripts under `sh` against a fake VPS (a directory tree with the package unpacked and fake `latchkey`, `docker`, `ssh-keygen`, `supervisorctl` and `stat` on PATH) instead of re-enacting them from the command text.

A reverse tunnel whose ssh does not reach the workspace container on its first try (its sshd still coming up, a stale bind clearing) no longer fails the provisioning pass that rewired it: supervisord keeps retrying the tunnel with the new target, so only the gateway has to come up for the pass to count as done.

`ensure_latchkey_installed`, `ensure_ram_backed_secrets_dir`, `reload_supervisor_programs`, `resolve_remote_latchkey_directory`, `SUPERVISOR_CONFD_DIR`, `MACHINE_LATCHKEY_SUPERVISOR_CONF_FILENAMES` and `GATEWAY_RUN_SCRIPT_FILENAME` are gone with the ad-hoc operations they drove; the gen-1 -> gen-2 cutover migrate that used them completes before this change deploys, so nothing replaced them for it.

The pinned latchkey CLI the package installs is 3.15.0, and the curl shims its `postinst` installs are `latchkey-curl-router` and `curl-impersonate` from the `imbue-ai/latchkey-curl-shims` v0.4.0 release, verified against the sha256 sums pinned in the package (the same pins the ad-hoc provisioning carried).

The desktop egress rules file (`~/.latchkey/proxyRules.json`) goes through the package too. The package's `gateway-run` creates it as `{}` when the machine has none and exports `LATCHKEY_DESKTOP_PROXY_CONFIG` and `LATCHKEY_DIAGNOSTIC_HEADERS=1`. `read-state` answers with the file, and `apply-state` installs a `desktop_egress_rules_json` entry just before the policy (`RemoteStateUpdate.desktop_egress_rules_json`, `RemoteMachineState.desktop_egress_rules_json`). So `MachineCredentials.refresh` and `set_permissions_and_desktop_egress_rules` still cost one remote command each.

Every `mngr-latchkey read-state` and `apply-state` command is run inside `commands_kept_out_of_logs`: its document carries the machine's secrets, so the host layer traces a stand-in naming the kind of command and its size instead of the command.

The desktop's encryption key no longer travels to a VPS. `read-state` hands the machine's credential store over as the machine holds it, still under the machine's own key (`RemoteStateRequest.is_credential_store_included`, answered as `RemoteMachineState.credential_store`), and `fetch_machine_state` re-encrypts it for the desktop locally with the new `Latchkey.reencrypt_foreign_store`, using the key the machine reports running under. The `desktop_key` and `probe_key` read entries (and `is_probe_key_accepted`) are gone.

When neither the machine nor this computer knows the machine's key, provisioning now tries the desktop's key against the store locally (`Latchkey.does_key_open_foreign_store`) instead of on the machine. The store rides in the provisioning pass's one existing read, which asks for it only when this computer has no key recorded for the machine, so the extra probe read is gone. The desktop's key goes to the machine only when it opens the store. Only upstream's own wrong-key refusal counts as "does not open": any other failure, such as a store in a format newer than the local CLI reads, fails the pass instead of abandoning the store. `provision_remote_gateway` takes the desktop's `Latchkey` in place of `latchkey_directory`.

Support for agents created before the one-gateway rollout (minds 0.3.11 and older), whose permissions-override JWTs pinned a machine to the desktop's key, is dropped. A machine with a `~/.latchkey` but no credential store now gets a fresh key of its own rather than the desktop's. The `HAS_LATCHKEY_DIR` read answer, which existed only for that case, is gone, and so is the rule that the package's `postinst` must not create anything under `~/.latchkey`.

## 2026-09-23

Stopped `mngr latchkey forward` from reporting an unreachable workspace host on every discovery cycle.

A VPS-backed workspace whose outer SSH endpoint refused the connection, had no route from this computer, or was off the network had its wiring failure logged at `ERROR` with a traceback every 30 seconds for as long as the supervisor ran. The same host was already reported only once per streak when its connection attempt timed out instead -- which of the two happened was decided by the network's answer, not by anything about the host. Both shapes are now treated alike: noted at `INFO` the first cycle, `DEBUG` after, and reported once at `ERROR` if the host is still unreachable ten cycles later.

Desktop egress is redesigned. A remote machine's gateway now injects the credentials itself and routes a chosen service's requests out through the user's computer, which only forwards them. The old `/via-desktop` route, which injected credentials on the desktop, is removed.

- The VPS gateway run script creates `~/.latchkey/proxyRules.json` as `{}` when the machine has none, and exports its path as `LATCHKEY_DESKTOP_PROXY_CONFIG`. The curl router reads the file on every request: each key is a latchkey service name, and a request that latchkey matched to a service whose value is truthy is sent through the desktop gateway. Every catalog service can be routed, including the ones latchkey matches by regular expression.

- The router does not match URLs itself. Latchkey names the service it matched a request to in the request header `X-Latchkey-Matched-Service`, and the router looks that name up in the rules file. Latchkey sets the header only with its diagnostic headers turned on, so the VPS gateway run script also exports `LATCHKEY_DIAGNOSTIC_HEADERS=1`. This needs latchkey 3.15.0, which added `LATCHKEY_DIAGNOSTIC_HEADERS` and is the version the VPS install pins.

- The desktop gateway runs with `LATCHKEY_PASSTHROUGH_UNKNOWN=1`, without which latchkey refuses a request that asks for no credential injection. The permission check still runs on such a request.

- Added `imbue.mngr_latchkey.desktop_egress`. `build_desktop_egress_grant` builds the permissions rule that lets one computer forward one scope: the scope's schema combined with a `customMetadata.deviceId` gate, with the `any` permission. The gate is on the device id because a host's permissions file is shared between the user's computers. `list_desktop_egress_grants` reads such rules back from the schema structure. `build_desktop_egress_rules`, `serialize_desktop_egress_rules`, `parse_desktop_egress_rules` and `is_service_routed` write and read the rules file.

- The rules file travels in the existing machine round trips, with no new remote command. `MachineCredentials.refresh` reads it together with the credentials and the policy (`FetchedMachineState.desktop_egress_rules_json`). `MachineCredentials.set_permissions_and_desktop_egress_rules` pushes a permissions snapshot and a rules snapshot in one remote command, after validating both.

- This computer keeps a copy of each remote host's rules file at `<latchkey_directory>/mngr_latchkey/hosts/<host_id>/proxyRules.json` (`store.desktop_egress_rules_path_for_host`), handled like its copy of the permissions file. A refresh adopts the machine's file, and removes the copy when the machine has no file. The machine is never seeded from the copy. `read_host_desktop_egress_rules` returns the copy's text.

- `FetchedMachineState` has a new required field `desktop_egress_rules_json`.

- Removed the `/via-desktop` route of the remote desktop-gateway proxy extension, the `MINDS_VIA_DESKTOP_URL_PREFIX` workspace environment variable (`ENV_MINDS_VIA_DESKTOP_URL_PREFIX`, `VIA_DESKTOP_URL_PREFIX`), and the `via-desktop-egress` baseline permission (`VIA_DESKTOP_PATH_PATTERN`). Permissions files of existing hosts keep a stale copy of that permission and its schema. It has no effect, because the route no longer exists.

- `DESKTOP_EGRESS_RULES_FILENAME` moved from `remote._machine` to `store`. In `account_scopes`, `SCHEMA_REFERENCE_PREFIX`, `ACCOUNT_METADATA_KEY`, `referenced_schema_name` and `custom_metadata_const_gate` are now public, so `desktop_egress` reuses them.

- The rules file format above is the one `latchkey-curl-shims` v0.4.0 reads, which is the release the VPS install pins. Its router also authenticates to the desktop gateway with the connected computer's gateway password and permissions-override JWT, read from the files provisioning already writes for the forwarding extension.

## 2026-09-22

# VPS workspaces reach their gateway over the docker bridge, not an SSH tunnel

A VPS-backed workspace now reaches its VPS-resident latchkey gateway at
`http://host.docker.internal:1989`. The gateway binds the VPS's docker bridge
address (never a public interface), and the workspace container resolves that
name to it through the `--add-host` mapping the VPS provider now creates every
container with. The VPS->container reverse SSH tunnel, the ad-hoc keypair it
authenticated with, and the `docker exec` that authorized it are no longer set
up for such containers.

Desktop-gateway workspaces are unchanged: their gateway URL stays
`http://127.0.0.1:1989`, reverse-tunneled in from the desktop.

Compatibility: a remote workspace whose `LATCHKEY_GATEWAY` still names its own
loopback keeps the reverse tunnel, now pointed at the bridge address the
gateway binds; neither the URL nor the container's mapping can change for the
life of the container. Provisioning recognizes such a workspace by a container
created without the mapping (`mngr-latchkey read-state` reports the
creation-time extra hosts of the container it is asked about), or by the
tunnel keypair an earlier provisioning minted on the VPS (which the read
reports too) -- which is how a workspace an older client created in a
container that already carried the mapping is told apart from a new one. A
tunnel once wired is therefore never dropped, and none is ever wired for a new
workspace: `apply-state` is handed a container to tunnel into only then, and
the package's `latchkey-tunnel` supervisord program is no longer autostarted.
Each kept tunnel is logged. The tunnel code (in the package's scripts and in
`remote/provisioning.py`) is marked `CLEANUP:` for removal once no such
workspace remains.

Rollout: the URL `mngr latchkey create-agent-env` emits is decided from the
gateway location alone, before the host exists, while the imbue_cloud fast path
adopts a pre-baked pool-host container as-is, so a pool host baked by an mngr
without the mapping hands out containers that cannot resolve the new URL. The
plugin therefore now hooks `mngr create` (`on_host_created`, after the host env
is written and before any agent starts): when the host's `LATCHKEY_GATEWAY`
names `host.docker.internal` but the container's `/etc/hosts` does not map it,
the URL is rewritten to `http://127.0.0.1:1989`, which is what provisioning
serves over the reverse tunnel it keeps for exactly such containers. Pool hosts
need no re-bake before a client carrying this change is promoted; until they
are re-baked, workspaces created on them simply keep the tunnel. Workspaces an
older client creates on a re-baked pool host in the meantime keep working after
that client upgrades (their tunnel is kept).

`prepare_agent_latchkey` / `mngr latchkey create-agent-env --gateway-location
VPS` emit the new URL. `imbue.mngr.primitives.OUTER_HOST_HOSTNAME_IN_CONTAINER`
(shared with the VPS provider, which builds its `--add-host` mapping from it)
names the host, and the docker-bridge address resolution the owner-exec VM
daemon already used moved to a shared `docker_bridge.py`; a provisioning pass
resolves the address once and binds both the daemon and the gateway to it (the
gateway reads it from `~/.latchkey/gateway.conf`, which `apply-state` writes
from the pass's document and the reverse tunnel reads as its far end).

Bump Latchkey to 3.15.0.

The scripts that move credential material to and from a machine no longer appear in the logs. Opening the permissions view of a workspace, or changing a permission, ran a script that embedded the credential store being moved, the key it was to be re-encrypted under, or the permission policy being applied, and the whole thing -- artificially large, since a base64-encoded store rides on one line -- was traced verbatim. Those runs are now scoped with `commands_kept_out_of_logs`, so the log records only what kind of script ran and how big it was. The same applies to the probe that tests a candidate encryption key against a machine's store.

Also removes `run_remote_latchkey`, which has had no callers since the machine updates moved into a single remote command.

# VPS provisioning fences the bridge-bound services off every interface but the docker bridge

The VPS-resident latchkey gateway and the owner-exec daemon bind the VPS's
docker bridge address, but binding an address does not bind an interface:
Linux delivers a packet for any local address whichever interface it arrives
on, so a packet for the bridge address that reached the VPS's public interface
(from a neighbour on the same segment, or a route for the private range
pointing at the VPS) landed on either service. Remote provisioning now installs
`nftables` on the VPS and, before starting either service, loads a policy (its
own table, `inet mngr_bridge_services`, at `/etc/nftables.d/mngr-bridge-services.nft`)
that drops traffic to their ports unless it arrives on `docker0` or on
loopback. Loopback stays open because the VPS's own processes reach the gateway
there (the compatibility reverse tunnel among them). The same policy confines
the other direction too: the bridge address is the container's default
gateway, through which it could reach every service the VPS binds on all
interfaces (its sshd, for one), so a new connection arriving on `docker0` for
any port but those two is dropped as well. Established traffic passes, so a
connection the VPS opens into the container keeps its replies, and the
container's internet traffic is forwarded rather than delivered to the VPS, so
it is unaffected. A systemd oneshot
(`mngr-bridge-services-firewall.service`) re-loads the policy at boot, ordered
ahead of docker and the owner-exec daemon -- which systemd brings back after a
reboot -- so the load runs before the daemon listens. A VPS the policy cannot be
applied on fails provisioning (retried on the next discovery cycle) rather
than starting the services unfenced.

The bridge interface name now lives in `docker_bridge.py` as
`DOCKER_BRIDGE_INTERFACE_NAME`, shared by the address resolution and the
policy. The latchkey e2e release test asserts the table is loaded on the VPS.

# Live forward owner record readable in one call

New `live_forward_owner()` in `forward_supervisor.py` returns the
`LatchkeyForwardOwner` record published by the forward that currently *holds* a
latchkey directory, or `None` when nobody holds it. It is the record-shaped
counterpart to `owning_forward_process()` and reads the directory once.

The record beside the ownership lock outlives the forward it names (it is
replaced only when the next forward claims the directory), so `load_forward_owner()`
alone cannot tell a running forward from one that was terminated. Consumers
waiting for the shared gateway's bound port -- the Minds desktop client -- use
this instead, which is what stops a supervisor restart from reading as a dead
supervisor.

The VPS gateway's curl shims now come from the `imbue-ai/latchkey-curl-shims` release (pinned at v0.4.0) instead of the datalib release. The gateway's `LATCHKEY_CURL` is `/usr/local/bin/latchkey-curl-router`, with `/usr/local/bin/curl-impersonate` beside it. The tarball is verified against a sha256 pinned in `mngr_latchkey.remote.provisioning` rather than a `.sha256` file downloaded from the same release. An already-provisioned VPS is switched over on its next provisioning pass; the old `latchkey-curl-dispatch` and `latchkey-curl-impersonate` binaries are left in `/usr/local/bin` and are no longer used.

## 2026-09-21

Pending permission requests now record when they were filed. `POST /permission-requests` stamps a `created_at` (ISO-8601 UTC) on the stored record, and `GET /permission-requests` returns it, so a consumer that reconnects later (such as the minds app after a restart) can tell requests that were already waiting from new ones. Records written before this change have no `created_at`.

- `imbue.mngr_latchkey.remote.provisioning` now exposes `LATCHKEY_CURL_PATH`, the curl the gateway run script this build writes names as `LATCHKEY_CURL` (the shim `ensure_latchkey_installed` installs), so the gen-2 migrate can give a run script written by another minds build a curl at the path it expects.

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

The detached-spawn unit tests now wait for the fake `latchkey` child to exit before they end. Two of them asserted on the capture file and returned while the child was still starting up, so when one of them was the last test in a session the session-end leak check found a live child at teardown and failed the run (on main as well as this branch).

## 2026-09-18

The latchkey supervisor now closes and drops the provider instances it replaces when a SIGHUP reloads its provider set. It swapped in a fresh context and relied on that alone to stop the old instances being handed out, which left them cached for the rest of the session, holding the host listings they had built and whatever a backend that releases resources on close was holding -- once more for every provider-set change the desktop client signalled.

Added `imbue.mngr_latchkey.device_metadata.build_device_metadata_env`, which builds the `DETENT_CUSTOM_METADATA` environment that makes a latchkey gateway report a device id to its permission checks as `customMetadata.deviceId`. A host's permissions file is shared between the user's computers, so this lets a rule apply on one computer only. Nothing writes such a rule yet.

## 2026-09-17

Test doubles that implement `OuterHostInterface.write_file` now default `is_atomic` to `True`, matching the interface: `write_file` is atomic by default on every host (the bytes are staged in a sibling temp file and renamed over the destination once complete). No behavior change in this project's own code.

## 2026-09-16

Bump Latchkey to 3.14.0.

Test-only: the two raw-capture rotation tests in `_spawn_test.py` now wait for the detached fake `ensure-browser` child to finish before returning, so the child no longer trips the leaked-process check at teardown (a flake seen in CI).

`POST /permission-requests` now accepts an optional absolute `target`: the permissions file an approved request writes its effect into.

Without it the effect always lands in whichever file the *caller's* extension context names, which is right for an agent asking for something and wrong for the desktop client asking on a workspace's behalf — that put file-sharing grants into the desktop's own admin permissions file, which declares no `latchkey-self` scope schema, so every later request against that file failed its permission check with a 403.

The override is honoured only when the caller's own context is the desktop client's admin permissions file. An agent's context names its own workspace's file and never matches, so one workspace cannot grant itself access through another's.

The rule that only the desktop client may file a permission request against another permissions file is now tested, in both directions: a caller running as itself is refused with a 403, and the desktop client's own context is honoured. That rule is the only thing standing between one workspace's agent and another workspace's permissions, and nothing exercised it.

## 2026-09-15

Bump Latchkey to 3.13.1.

## 2026-09-14

Fixed two spurious failures that could abort VPS gateway provisioning after the work had actually succeeded. Both came from the same cause: a command sent with `execute_idempotent_command` is retried on a transient SSH error (a reloaded sshd, a dropped channel), including when the far side had already run it, so any command that consumes a file fails on the replay.

Writing minds' `config.json` onto the VPS could fail with `Failed to move temp file to final location ... mv: cannot stat '.config.json.<id>.tmp': No such file or directory`, even though the config had been written correctly. The fix is in `imbue-mngr`'s atomic write; see that project's entry.

Installing the desktop-gateway proxy extension had the same problem in its own compare-and-swap script: on a replay the staged copy was gone, which the content comparison read as "differs", sending it into a rename with no source. It now skips the swap when the staged copy has already been consumed, and still fails loudly if the staged copy and the installed extension are both missing.

Bump Latchkey to 3.13.0.

Fixed `mngr latchkey forward` never following a remote workspace that was restored onto new coordinates by someone other than the client itself (an operator `minds-admin cutover migrate` or `workspaces start`, a watchdog re-drive, a start from another device). The supervisor cached the host's gateway route, the provider's lease listing, the desktop-to-VPS tunnel and the provisioned marker for its whole lifetime, so it kept wiring the workspace's gateway against the old VM until the desktop app was restarted. The container endpoint discovery reports each cycle is now compared against the one the route was resolved for: a change drops the cached route, refreshes the provider's listing, removes the tunnel to the old outer endpoint by its exact key, and re-runs the idempotent VPS gateway provisioning, all within one discovery cycle. A desktop-to-VPS tunnel failure also marks the cached route stale so the next cycle re-resolves instead of retrying a dead endpoint forever; the stale route stays cached as the baseline the endpoint comparison needs, so a migration whose old VM stops answering before the new coordinates are reported is still recognized and re-provisioned (a failure of this computer's own end of the tunnel, such as trust material missing on disk, keeps the route in use since it says nothing about where the host is).

The remote provisioning module's software installer (`ensure_latchkey_installed`), RAM-backed secrets directory check (`ensure_ram_backed_secrets_dir`) and supervisord reload helper (`reload_supervisor_programs`) are public, along with the supervisord drop-in directory and program names, the gateway wrapper and tunnel key filenames, the extensions directory name, the tmpfs secrets directory and its four filenames, and new tuples naming the machine-owned latchkey files by where they live (`MACHINE_LATCHKEY_DISK_FILENAMES`, `MACHINE_LATCHKEY_SUPERVISOR_CONF_FILENAMES`, `MACHINE_LATCHKEY_TMPFS_FILENAMES`, and the gateway's required pair). The gen-1 to gen-2 workspace migrate in `minds-admin` reuses them to install the same pinned software and carry a machine's whole latchkey state onto a fresh VM instead of shipping a second installer; a new test pins the tuples to exactly what provisioning writes. No behavior change for the desktop's own provisioning.

## 2026-09-12

Integration branch for the imbue_cloud slice-fleet generation 2 program (see the other projects' `mngr-gen2-combined.md` entries). The latchkey-side change it carries:

Gen-2 small follow-ups: the VM-resident owner-exec daemon pin (`OWNER_EXEC_VERSION` in `owner_exec_vm.py`) moves from v0.2.1 to v0.2.2, which never authorizes an `authorized_keys` line carrying options (`command=`, `restrict`, `from=`, `cert-authority`); v0.2.1 stripped the options and granted such keys unrestricted exec. The default-workspace-template's in-container pin moves in lockstep.

## 2026-09-10

Bump Latchkey to 3.11.1.

Transient SSH failures while wiring a remote workspace's latchkey gateway (the desktop-to-VPS tunnel, the desktop-gateway reverse tunnel, and VPS gateway provisioning) are no longer reported as errors on every occurrence. Discovery already retries each of these steps every cycle (30s by default), so a connection reset, a dead transport, a handshake that found "No existing session", or an authentication timeout is now logged at info the first time (debug on later cycles) and left for the next cycle to retry. The provisioning worker no longer lets such an exception escape to the concurrency group, which is what produced an error-level traceback (and a Sentry event) for every blip.

A host that keeps failing with transient errors for `TRANSIENT_FAILURE_REPORT_THRESHOLD` (10) consecutive cycles while reporting as running is reported as an error exactly once, traceback included, so a genuine misconfiguration (a rotated key, a firewall) still surfaces instead of being retried silently forever. Retrying continues either way, and a later success (or the host stopping) resets the streak so a new outage reports afresh.

Failures that retrying cannot fix (trust material missing on this device, a rejected key, a malformed file) are still reported as errors immediately. The classification is `discovery.is_transient_remote_wiring_error`, which walks the error's cause chain and reuses mngr's `is_transient_ssh_error`.

Added the foundation for user-created custom services: third-party services an agent can ask for at runtime, rather than only the ones this package ships.

A custom service lives as a single `custom_`-prefixed entry in latchkey's own `config.json` under `registeredServices`. There is no second store: its label, the Detent scope it exposes and the domain that scope pins all follow from that entry, so nothing can drift from the file that actually drives the gateway. The new `custom_services` module owns the accepted domain grammar, the service name derived from a scheme and a domain (`https` + `api.example.com` -> `custom_https_api_example_com`), the registration built for it, and the projection of those entries back into catalog entries.

`ServicesCatalog` now takes an optional `latchkey_directory` and overlays that install's custom services on the shipped catalog, so one projection feeds every surface that reads it -- the permission dialog, the Permissions tab, the Connectors page, and the credential subset shipped to a VPS. The gateway extensions are unchanged: `core` materializes the same overlaid catalog into `extensions/services.json`, so they keep reading one file in one shape.

The bundled `claude-ai` service is untouched, and existing workspaces need no migration: the overlay only ever adds services, never displaces one.

The gateway's `permission-requests` extension accepts a new `custom-service` request type: an agent names a domain and, optionally, a cookie sign-in, and approving both registers the service and grants that workspace access to it. The domain must be a plain hostname -- no scheme, port, path, wildcard, underscore or IP literal, no reserved suffix. There is no display-name field: a custom service is labelled by its origin, the one string that cannot misdescribe what it reaches.

The grant carries the scope's own definition. Unlike a `predefined` grant, which names a scope detent already knows, a custom service's scope is one this package invents, so the grant writes that scope's schema alongside the account-gated schema referring to it. The approved permissions file therefore has nothing to resolve elsewhere, which matters because an unresolvable reference fails detent's whole check for that host rather than just the rule that needed it.


A later `predefined` grant naming a custom service's scope now carries that scope's definition too. Previously only the approval that created the service wrote it, so a second workspace granted the same scope got a rule referring to a definition its own permissions file did not have -- and an unresolvable reference fails detent's whole check for that host, so that workspace lost every permission rather than just this one. The catalog entry carries the definition, the desktop reads it from there, and building such a grant without it is refused outright.

Custom services registered while minds is running are now visible immediately. The services catalog cached its first read for the life of the process, so a service approved mid-session worked at the gateway while every surface that names services -- the Permissions tab, the Connectors page, the scope lookup behind a permission request -- kept reporting it absent until a restart.

A custom service created after a VPS-backed workspace was provisioned now reaches that VPS. This computer's half of the machine's `config.json` -- the hidden built-in services and every registered service -- now rides every connect as a whole snapshot, installed ahead of the credential in the same single script that merges it into the machine's store, the way the policy already travels; a gateway with no entry for a service cannot route a request to it. Nothing else on a machine writes that file, so the newest snapshot is always right, and a service deregistered here disappears from the machine on its next connect. Previously the grant and the credentials were synced there while the registration was not, so the gateway never injected them.

`GET /permissions/available/<service>` now accepts a custom service's name. The endpoint constrained names to letters, digits and hyphens, so the underscore in a derived name (`custom_https_api_example_com`) was rejected as malformed -- blocking the documented way for an agent to discover a scope before asking for it.

A `custom-service` request names a `scheme`, `https` or `http` -- the latter for services on private networks that have no certificate. The service is registered at that origin, and the grant pins the scheme as well as the domain -- detent decomposes a request into both -- so it covers exactly the origin the user approved. A sign-in URL may use either scheme; it only has to belong to the domain. There is no cap on how many cookies a sign-in captures beyond latchkey's own rule of at least one.

A custom service's domain is now any bare ASCII hostname -- dot-separated labels of letters, digits and hyphens, so a single label, a private suffix such as `.internal` or `.local`, or an IPv4 address are all fine -- rather than a hand-written approximation of public DNS. The only refusals are anything beyond a bare host (a scheme, port, path, underscore or wildcard), a name longer than DNS allows, and the gateway's own address. The service name carries the scheme (`custom_https_example_com`), so an http and an https service on one domain are two services, and the grant pins the scheme as well as the domain.

A `custom-service` request for an origin this computer already has a service for is now accepted rather than refused. A second workspace wanting an origin an earlier one connected sees exactly what the first did -- its own gateway has no service for it -- so this is the only request it can make; it used to be answered with a 400 telling the agent to send a `predefined` request naming the scope instead. The desktop now keeps the existing registration, sign-in included, and connects the workspace to it. With that, the `permission-requests` extension decides nothing about a custom service: it checks the request's shape, stores it and lists it, and the desktop writes the grant through `POST /permissions/rules` exactly as it does for a `predefined` one. `/approve` of a custom-service request applies nothing and takes no override.

`custom_services.domain_warning` classifies a valid domain that still deserves a second look: a reserved name nothing answers to, a name the workspace machine's own network resolves (`localhost`, a single label, `.local`, `.internal` and the like), any IP address, or a punycode label that could look like another site. Advisory only -- these replace the hard refusals that were dropped earlier -- and the desktop is the only reader.

A `custom-service` request's `login` object describes a browser sign-in the way `latchkey services register` takes it: `url`, `flow` (`cookie-capture` or `token-capture`) and `flow_params`, the flow's own parameters keyed as latchkey keys them, in place of the earlier cookie-only `login_url`/`cookie_url`/`cookie_keys`. The parameters are deserialized against the flow's schema on both the gateway and the desktop -- an unknown key is refused, and every URL among them has to be on the requested domain -- and then registered exactly as sent. `token-capture` becomes usable as a result.

## 2026-09-09

Remote workspaces now keep working when the user moves to another computer. Previously a remote workspace's VPS gateway ran on the *creating* computer's derived listen password, and the forwarding extension replayed the caller's password header on the hop back to the desktop, so both halves broke as soon as a second computer provisioned the machine: it overwrote the tmpfs listen password with its own value, which the workspace -- whose `LATCHKEY_GATEWAY_PASSWORD` is written once, at `mngr create` -- could no longer present.

- The machine's gateway listen password is now the machine's own secret, adopted rather than decided, exactly as its encryption key already was. A provisioning pass takes the password the machine is running under, falls back to a new durable record in the machine store (`hosts/<host_id>/machine_gateway_password`) for a machine whose tmpfs a reboot wiped, and only seeds this computer's derived password into a machine that neither is true of -- so the workspaces on a machine keep authenticating with the password they were created with, whichever of the user's computers is running.

- The desktop gateway's own password is now a separate secret, written to the machine's tmpfs (`/run/mngr-latchkey/desktop_gateway_password`) alongside the desktop-target permissions-override JWT and overwritten by every provisioning pass, since both belong to whichever computer is currently connected. The forwarding extension presents them on the hop to the desktop instead of replaying the caller's own password header (which authenticates the caller to the VPS gateway, and is now a different value).

- Fixed the VPS gateway's launch wrapper to pass its listen port as `LATCHKEY_GATEWAY_LISTEN_PORT`, the variable latchkey actually reads. The old `LATCHKEY_GATEWAY_PORT` was ignored, so the gateway bound latchkey's default port -- which is the port we intended anyway, so nothing was broken, but changing it would not have moved the gateway.

- The forwarding extension reads both desktop-owned secrets from their files afresh on every request it proxies (they are handed to it as `LATCHKEY_EXTENSION_DESKTOP_GATEWAY_PASSWORD_FILE` / `..._PERMISSIONS_OVERRIDE_FILE`), so a pass from a new computer takes effect without restarting the VPS gateway or the workspace it serves, and a proxied request can never present the secrets of a computer that is no longer on the other end of the tunnel. Their absence is no longer a gateway launch failure either: the desktop-owned routes answer HTTP 503 saying so, while third-party calls, which need neither secret, keep working.

`mngr latchkey forward` now takes an exclusive lock on its latchkey directory, and the supervisor identifies a running forward from that lock rather than from the forward's command line.

This fixes a silent failure for latchkey directories containing a space. The supervisor used to find a forward by reading its command line and pulling `--latchkey-directory` out of it. `mngr` overwrites its own process title on every invocation, so that command line arrives as a single fused string with the remaining argv slots blank -- the shape essentially every running `mngr` presents, not an edge case -- and a path containing a space was truncated at the first space. Reaping is scoped by that path, so the supervisor stopped recognising the forward it had itself spawned and left duplicate gateways running, with no error reported. Nothing constrains `--latchkey-directory` to a space-free path: `/Users/Jane Doe/...` produces one, as does any directory under macOS's `~/Library/Application Support/`.

**Ownership.** Each forward holds the lock for its whole life -- a `filelock` read/write lock, whose read side lets a probe ask who owns a directory without contending with another probe -- and writes its pid into a record beside it, so "is a forward running here, and which process is it?" is answered from the directory itself. The lock file's location supplies the scoping, so a sibling profile's forward can never be mistaken for this one, and the kernel releases the lock however the holder exits, so a crashed forward leaves nothing behind for the next one to clear. Because a lock is released only on exit, a held lock has a live owner behind it, and the record naming that owner is cleared and rewritten the instant the lock is taken, so a probe landing between those two waits for the new stamp rather than reporting no owner. Nothing is parsed out of a process title, and no timestamp decides whether a forward is alive.

**The owner is handed over as a live process, not a pid.** Everything that acts on the owner -- stopping it, reaping the gateway and reverse tunnels underneath it -- now works from a handle carrying the identity captured when ownership was read. Signalling that handle cannot reach a different process that inherited the pid in the meantime, which a pid re-resolved at the moment of the kill cannot rule out. The gap this closes is widest in `stop`, which walks the forward's whole descendant tree before it signals anything, and sharpest in the provider-set bounce, which sends SIGHUP -- a signal whose default disposition is to terminate, so a stranger that inherited the pid would have been killed rather than bounced.

**A directory whose owner is mid-publish is no longer read as unowned.** A forward clears the owner record and rewrites it in the instant after taking the lock. A probe landing in that gap saw a held lock and no record, concluded the directory was unowned, and its caller spawned a second forward -- which the real owner then refuses, failing a caller whose forward was perfectly healthy. Such a probe now waits briefly for the record to land. Only a holder that dies between taking the lock and stamping it still reads as no owner. `mngr latchkey gateway-info` asks the same probe, so it too reports a forward caught mid-publish as still binding its port rather than telling you to start a forward the running one would refuse.

**A second forward for the same directory is now impossible** rather than reaped after the fact: it refuses to start while another owns the directory. Only a lock held by another process reads as "already owned" -- a directory that cannot be claimed for some other reason, such as a filesystem that cannot take the lock or a pid stamp that cannot be written, reports that failure instead of blaming a forward that does not exist. A claim that fails partway releases the lock on its way out, leaving the directory genuinely unclaimed.

**A spawned forward is confirmed to own the directory before the supervisor reports it**, and terminated if it never does. A child that refuses the directory, or dies before claiming it, used to have its pid returned and remembered as the running supervisor's, where a later stop would signal whatever held that pid by then. One that starts but never claims the directory is now killed rather than orphaned: it recorded nothing, so no later call could have found it. What is waited for is an owner rather than that child specifically, because the check for an existing owner and the spawn are not one step across processes: a child that finds the directory claimed in between refuses it within a second, and the forward that won is adopted instead of being waited past to a failure.

**One record, not two.** The lock's owner record carries the gateway port as well as the pid, and `latchkey_forward.json` is no longer written at all. Everything a live forward publishes now lives in one place, written by the process holding the lock and read by everyone else.

**Upgrading.** A forward from a build without the lock holds none, so nothing lock-based can see it; only the `latchkey_forward.json` it wrote can. Everything needed to recognise and clear one lives in a single module, called once when a supervisor starts, so the first launch of the updated app replaces it. That record goes on that launch whether or not it still parses, so a damaged one is not left to be re-read and warned about forever. The module is marked for deletion once no such forward can still be running.

**`stop` and `ensure_running` are serialised against each other.** An embedder drives both from more than one thread, and `ensure_running` now holds the supervisor's mutex across the spawn and the wait for that child to claim the directory; a `stop` that ran alongside it would kill the child being waited on, failing a spawn that was healthy.

**`ensure_running` adopts a live forward rather than replacing it.** The forward owns the gateway's bound port and every per-agent reverse tunnel, which a replacement would drop; `restart` is the verb for replacing one, though a second embedder racing for the same directory can still claim it first. Previously `ensure_running` replaced a forward whose record was missing, on the theory that it might be stale -- but every forward recorded itself, so a missing record only ever meant a `stop` that had failed to kill, which the lock now makes far less likely.

The README gains a troubleshooting note for the case where two forwards do end up sharing a latchkey directory: list them with `ps -eo pid,args` and kill the stray pid, whose teardown takes its shared gateway, `mngr observe` child and reverse tunnels with it.

## 2026-09-07

Bump Latchkey to 3.11.0.

Per-machine latchkey credentials: each machine -- the user's computer, and each remote host's VPS -- now owns its own credential store and refreshes its own OAuth tokens, so a remote workspace keeps working while the computer that manages it is off, and two computers no longer race to rotate the same token. The desktop renews nothing on a machine's behalf.

Each remote host's per-host directory is now a `LATCHKEY_DIRECTORY` of its own (a "machine store"): it holds this computer's mirror of that machine's credentials, the machine's cached policy, the mirror's format stamp and the machine's recorded encryption key, and shares the desktop's config, browser session, encryption key and `last-daily-count` usage stamp by symlink. That is what lets one browser sign-in be reusable across machines, gives the ordinary offline `latchkey` reads a directory to read, and keeps the once-a-day usage ping from being counted once per remote host.

Credentials shipped to a machine come from that machine's own store rather than from a filtered copy of this computer's, and are re-encrypted with that machine's key on the way out. A handover names the one *account* that was connected (`latchkey auth re-encrypt --account`, Latchkey >= 3.10.0), so signing in to a second account of a service cannot write over the machine's copy of the first -- which the machine may have refreshed since this computer last read it. A machine store that has never held anything is seeded once from this computer's credentials, so existing remote hosts keep what they already had.

A machine's encryption key belongs to the machine: the tmpfs copy its gateway runs under is the source of truth, and each computer's recorded copy is only a durable mirror of it (kept so a rebooted machine can be handed its key back). Provisioning adopts the key the machine is already running under instead of deciding one, so two installs can manage the same remote workspace without re-keying it out from under each other. When neither side knows a key: a machine with no credential store gets a fresh one; a store that verifiably opens under the desktop key (a machine provisioned before per-machine keys, whose agents may carry a JWT the gateway validates with a key derived from it) keeps it; and a store written under a key held only by a computer that is gone is abandoned -- removed, with a fresh key minted -- since its services can be signed in again.

`MachineCredentials` (in the new `imbue.mngr_latchkey.remote.credentials`) is a plain synchronous API for one such machine: `connect_service`, `disconnect_account`, `set_permissions`, `connect_service_with_permissions` and `refresh`. It is built for the duration of one exchange -- the caller opens the machine's outer host, does what it came to do, and lets both go -- and every method costs a single remote command: one POSIX `sh` script that expands `$HOME` itself, carries its payload base64-encoded, verifies the machine's tmpfs key against a SHA-256 of the recorded key (so the key itself never rides in the command line), does the work atomically, and reports its outcome on its last line. Each method either succeeds before returning or raises `RemoteGatewayError` carrying the machine's own reason, so an embedder can block a user's click on it and report what the machine said. Nothing is queued and nothing is applied later.

`refresh` reads both halves back in one command -- the machine's credential store, re-encrypted for this computer, and the policy its gateway is enforcing -- and settles which side of a disagreement wins. For both halves the answer is the machine: the credentials obviously (only it can rotate the tokens it holds), and the policy because the user may have several computers and any of them can push a grant, so a computer that treated its own copy as the truth would revert what another one granted every time the policy was read. This computer's copies are caches. The one write `refresh` makes toward the machine is the seed: a machine with no policy at all permits everything, so it is handed this computer's copy. What keeps the adopt safe is that the copy here is never edited *without* being pushed in the same operation -- an unpushed edit is discarded by the next refresh rather than eventually carried over, so a writer that cannot push has to say so. `finalize_host_permissions` is the one deliberate exception: what it promotes is a deny-all baseline rather than a grant, so pushing it would overwrite whatever the machine is already enforcing.

A push that *brings* credential material (a connect, or a grant) refuses a machine that has been re-keyed from another computer, since a bundle encrypted with the recorded key would leave the gateway a store it cannot read. A disconnect and a permissions snapshot land anyway: a clear brings nothing of this computer's to write (and refusing would leave a credential the user signed out of sitting on the machine), and a policy is not encrypted at all. A machine that lost its RAM-backed key to a reboot is handed the key back and the script is run once more -- the only case that costs a second round trip. A push too large to fit one remote command (past ~100 KB, which no real policy or credential bundle approaches) is refused with that reason.

Reading a machine no longer fails once its last connector is disconnected. Upstream `latchkey auth clear` empties the machine's credential store rather than deleting the file, and `latchkey auth re-encrypt` refuses an empty store ("No stored credentials found to re-encrypt"), so the read failed. A read now recognizes that answer as "this machine holds nothing" -- clearing this computer's mirror, as it does for a machine that never held anything -- while any other reason the machine gives still fails the read, with the machine's own wording.

`register_agent_for_host` reports whether it changed the canonical permissions file, so a caller that can reach the host's machine pushes the file when it did: `mngr latchkey register-agent` does so for a host with a machine of its own (a remote workspace whose gateway was provisioned from this computer), failing with the machine's reason -- and leaving the local registration in place -- when it cannot. A remote gateway checks the `latchkey-self` baseline grants and the additional-service schemas a registration brings up to date against its own copy, so that push is what makes a newly-shipped baseline grant live on an existing remote workspace.

`mngr latchkey forward` no longer syncs credentials, no longer watches the desktop's credential store or the per-host permissions files, and no longer reconciles every known remote host at startup. `watchdog` is no longer a dependency. It still seeds (or adopts) a freshly-provisioned machine's `permissions.json` as part of the provisioning pass -- a latchkey gateway with no permissions file permits everything, so that one cannot wait for a user to open a UI.

The periodic pass that took credentials away from a machine for having no permission behind them is gone. That rule existed to stop the desktop's credentials from landing on machines with no grant for them; nothing reaches a machine now except what was connected for it, dropping a machine's grants ("Revoke all") is already a separate action from signing an account out of it, and a credential outliving its grants is what makes re-granting them a click rather than a fresh sign-in.

Removed the plugin's data-format migration mechanism (`imbue.mngr_latchkey.migrations`) and the `Latchkey.initialize()` step that ran it. Its last migration shipped in July 2026 and existing installs have long since run it; it was also the one writer of a permissions file that could not push what it wrote, which the ownership rule above does not allow. A file carrying keys this build does not model still loads and still sheds them on the next save (`LatchkeyPermissionsConfig` is `extra="ignore"`), but nothing rewrites a file whose *rules* are in an old shape any more. A future replacement with per-workspace granularity has to carry its version on the wire alongside the policy, not only in a file on disk: a local-only stamp cannot help a policy that arrives from another computer.

The remote-machine modules now live together under `imbue.mngr_latchkey.remote`, with a two-module public surface: `remote/provisioning.py` (standing up and reconciling a VPS gateway) and `remote/credentials.py` (a machine's credentials and policy as seen from this computer), plus `remote/errors.py` for `RemoteGatewayError`. The old `remote_gateway` module is gone, its internals now living in underscore-prefixed modules under `remote/`; `local_credentials_path` moved to `core`, and the unused `accounts_in_machine_store` / `accounts_by_service` helpers were deleted.

A reconcile resolves the machine's `~/.latchkey` location once, lazily, and passes it to every step instead of each step running its own `echo "$HOME"` on the VPS -- previously up to five extra remote round trips per pass, and none at all for a pass that only pushes. Provisioning also no longer recreates, on every reconcile, the symlinks that let a pre-one-gateway-rollout workspace's baked-in permissions-override JWT resolve on its VPS: those symlinks are made once and live on the VPS, so any such workspace that has been reconciled since the rollout keeps working, and the temporary shim (with the `opaque_handles_for_host` helper it needed) is gone.

The scratch directory a transfer stages its bundle in on the machine (`~/.latchkey/mngr-latchkey-transfer.<pid>`) is per-script rather than one shared name, so two transfers overlapping on one machine -- a Permissions tab refresh racing a grant, or another of the user's computers pushing -- no longer delete each other's staged material. Each script also sweeps any such directory older than an hour, so one left behind by a script that died without running its cleanup does not accumulate.

## 2026-09-01

Bump Latchkey to 3.10.0.

## 2026-08-28

Bump Latchkey to 3.9.0.

Inline the detent schemas of additional (custom) latchkey services -- currently `claude.ai` -- into every permissions file minds writes, and drop the shared `minds_shared_schemas.json` file that host files used to pull in via detent's `include` directive.

Detent resolves a bare `include` relative to the directory of the file that references it, and a host's permissions file is read through more than one directory: its canonical `hosts/<host_id>/` path, the opaque handle under `permissions/` that a desktop workspace's JWT names, and `~/.latchkey/permissions.json` on a VPS. The shared file therefore had to be materialized next to each of them, and an include that failed to resolve failed the *whole* permission check for that host rather than just the rule that needed it. A self-contained permissions file has nothing to resolve.

The agent baseline now carries the schemas (`baseline_permissions.ADDITIONAL_SERVICE_SCHEMAS`), and `reconcile_baseline_permissions` refreshes them on hosts that already exist -- overwriting by name, so a package update to a custom service's schema wins over the copy an older build inlined. `LatchkeyPermissionsConfig` no longer models `include`; an `include` written by an older build is ignored on load and dropped on the next save. `Latchkey.initialize()` no longer materializes the shared file, and `remote_gateway` no longer ships or symlinks it onto a VPS.

No data-format migration is needed. A permissions file written by an older build keeps its `include`, and that is inert unless one of its rules actually resolves through it -- which only happens on a host that has been granted a custom-service scope. Such a file is repaired the next time an agent is registered on the host: `reconcile_baseline_permissions` inlines the schemas, and the `include` key disappears on the following save because the model no longer reads it.

A leftover `minds_shared_schemas.json` may remain in the plugin's permissions directory (and in `~/.latchkey` on a VPS). It is unreferenced once a host file has been repaired, and is safe to delete.

## 2026-08-27

Bump Latchkey to 3.8.0.

## 2026-08-26

The remote (VPS) gateway's on-host locations are now public constants: `REMOTE_LATCHKEY_DIR_NAME`, `REMOTE_GATEWAY_LOG_FILENAME`, and `REMOTE_TUNNEL_LOG_FILENAME` in `remote_gateway`. Minds' bug-report diagnostics tail the gateway logs at these paths over `mngr exec --outer`, so the paths are one shared source of truth instead of duplicated strings.

Fixed a stale docstring that pointed the forward supervisor's structured log at `<plugin_data_dir>/forward_logs/events.jsonl`; it lives at `<plugin_data_dir>/events.jsonl`.

# Service-name pattern made public

`SERVICE_NAME_PATTERN` in `additional_services.py` (previously private) is now
the documented canonical service-name shape. The minds embed contract's
wire-side service-name check mirrors it, and a new alignment test in
apps/minds (`test_service_name_alignment.py`) fails CI if the two rules drift.
No behavior change.

## 2026-08-25

Bump Latchkey to 3.6.0.

Reworded latchkey gateway docs, comments, and one error message ("A VPS latchkey gateway requires a tunneled host") from the minds-level workspace vocabulary to host/agent terms while merging the machine/workspace naming cleanup branch (`mngr/fix-machine-workspace-naming`, PR #436) into up-to-date main. No behavior change.

## 2026-08-24

Fixed the discovery handler tearing down and re-dialing the desktop-to-VPS latchkey tunnel on every 30s discovery cycle.

The VPS branch of the per-agent setup unconditionally removed every reverse tunnel tagged with the agent before (re-)establishing the desktop-to-VPS extension tunnel. That removal exists to clear a stale desktop-to-container tunnel (which would otherwise squat the container port the VPS-to-container tunnel must bind), but since the desktop-to-VPS tunnel was retagged with the same agent instance key, the removal also dropped the tunnel established on the previous cycle -- cancelling the port forward, often closing and re-dialing the SSH connection, and leaving a brief window per cycle in which the VPS-side extension port was unbound (in-flight desktop-owned extension requests could fail with connection-refused).

The cleanup is now keyed by the container endpoint (via the tunnel manager's new `remove_reverse_tunnel`), so it removes exactly the stale desktop-to-container tunnel when one exists and is a no-op in the steady state; the desktop-to-VPS tunnel now persists across discovery cycles. Stop/destroy teardown still removes all of an agent's tunnels by agent tag, unchanged.

## 2026-08-21

No user-visible change. `SSHTunnelError` now requires the tunnel phase it was raised from (so a caller can tell a failure on this device from one that reached the agent's host), and latchkey's reverse-tunnel test constructs one, so it passes the new argument.

The desktop now periodically renews the expiring credentials that remote (VPS-backed) workspaces hold a copy of, so a connection like Gmail no longer stops working an hour after you last touched it.

Remote workspaces call third parties from a gateway on their own VPS, which runs with credential refresh disabled so it never races the desktop to rotate a shared OAuth refresh token. That left the desktop as the only party able to renew one -- and the desktop only renewed lazily, when a request happened to pass through it. A service used *only* by remote workspaces therefore had no reason to ever be renewed: its access token expired an hour after whatever last touched it, and every call from the workspace failed with "credentials are expired" until the user opened that service's permission dialog, which renewed it by side effect for another hour.

The renewal loop probes only what it has to: the services some known remote host is actually granted, narrowed -- by an offline, local read of the credential store -- to those whose stored accounts are of a kind that can expire and be renewed, namely OAuth connections like Gmail or Dropbox, and Zoom. Static tokens never expire, so they are left alone, and a user with only local workspaces makes no third-party requests for this at all. Renewed credentials reach each VPS over the existing sync, which already watches the credential store.

This shortens the failure window rather than closing it. latchkey renews a token only once it has already expired, so a remote workspace can still see up to five minutes of expired-credential failures per token lifetime -- except right after the app starts, where the first renewal runs as soon as a remote workspace is known rather than five minutes later, since nothing was renewing anything while the app was closed. And renewal happens on the user's own machine: the supervisor that runs it is detached and outlives the desktop app closing, but a machine that is asleep or off renews nothing, so a workspace still loses access an hour after its owner shuts the lid. Letting a workspace renew its own credentials is the real fix and is being designed separately.

## 2026-08-19

The owner-exec VM installer's curl downloads now pass --retry-all-errors, so protocol-level failures (e.g. the intermittent HTTP/2 PROTOCOL_ERROR from GitHub's release CDN) are retried instead of failing the install outright. Integrity is still guarded by the sha256 check. The same fix is applied to the paired default-workspace-template's system/scripts/install_owner_exec.sh.

## 2026-08-18

- Workspaces can now route an outbound third-party request through the latchkey gateway on the user's own computer, so it reaches the destination from a residential connection rather than from the workspace's VPS. Some destinations (Cloudflare-fronted ones especially) block datacenter IP ranges outright, where TLS impersonation does not help because the address itself is refused.

- Every workspace now gets a `MINDS_VIA_DESKTOP_URL_PREFIX` env var naming the prefix to put in front of a target URL to ask for this. It is `https://latchkey-self.invalid/via-desktop` for VPS-gateway workspaces and empty for desktop-gateway ones, whose gateway already runs on the user's machine -- so in-workspace tooling concatenates it without branching on topology and gets the right behavior in both.

- The VPS gateway's forwarding extension now serves that prefix, handing the request to the desktop gateway's own outbound proxy with the prefix swapped for `/gateway/`. The target must be an absolute `http(s)` URL and is passed through byte-for-byte; a malformed one is rejected on the VPS with a clear 400 rather than failing opaquely a hop later.

- This route grants nothing new. Credentials are injected, and the permission check runs, on the desktop against the same per-host permissions file a direct request uses, so a service the user has granted is reachable both ways with one grant and a service they have not granted is reachable neither way. The agent baseline opens the route by default and it never appears as its own permission prompt.

Added: a curated `service_display_name` in the services catalog, naming a service as a whole rather than one of its scopes.

Catalog display names are per *scope*, and a service exposing several disambiguates each one with a parenthetical (`GitHub (REST API)`, `GitHub (GraphQL API)`, `GitHub (git)`). One credential backs every scope, so readers that name the whole connection had to fall back to the first scope's label -- reading as though the other scopes sat beneath that one. The generator now curates the service's own name beside the scope labels, and `services.json` carries it for the services that need one (`github`, `gitlab`); everything else takes its single scope's label verbatim, so `Notion (MCP)` is unchanged.

## 2026-08-16

Latchkey's per-agent tracking is now instance-scoped (`<agent_id>@<host_id>`): reverse-tunnel tags, pending-setup coalescing, and the destroyed callback (which now carries the host id) all key on the (host, agent) pair, so destroying or stopping an agent on one host never tears down tunnels for a same-id agent on another host (agent ids are unique per host, not globally).

The latchkey discovery stream consumer now uses mngr's deduplicating schema-mismatch warner: discovery lines written by a different mngr version are skipped with one warning per distinct failure (instead of one warning per line), and additive fields from newer versions parse cleanly. Part of the forward-compatible event parsing work (mngr-internal#422).

- The remote-gateway provisioning pass now also installs and starts the
  VM-resident owner-exec daemon (`owner_exec_vm.py`) on every remote outer
  (imbue-cloud slice VM or VPS), pinned to owner-exec v0.2.1. It runs as root,
  binds the audience `vm:<host-id>`, verifies request signatures against the VM
  root's `authorized_keys`, and signs responses with the VM's SSH host key --
  giving web-only workspaces SSH-equivalent authority to configure components
  that run outside the container. It binds ONLY the internal docker-bridge
  address the container reaches it at (never a public/wildcard interface): an
  unresolvable bridge address fails provisioning rather than binding `0.0.0.0`.
  A no-op on a local outer.

Latchkey's reverse-tunnel SSH info now carries the explicit `known_hosts_path` that host SSH info gained in this release: the discovery-stream conversion and the VPS gateway route resolution both populate it, so latchkey's tunnels verify host keys against the named pin file instead of relying solely on the key-sibling `known_hosts` convention.

## 2026-08-15

The latchkey discovery handler now warns (once per host, per episode) when a host reports `UNAUTHENTICATED` instead of skipping it in silence (re-landed from PR 382). Such a host is up and usually serving the workspace fine -- only the outer sshd rejected this machine's key -- and previously the sole symptom was every latchkey call from that host's agents failing with connection-refused, with nothing in any log connecting the two. The skip itself stands (there is no safe gateway to wire); a host that authenticates again and is then rejected again warns afresh.

## 2026-08-14

Custom services (the ones minds ships itself, currently `claude.ai`) now work in remote workspaces. Their latchkey registration is written into the config of every gateway that serves minds agents, including the VPS-resident one, so it travels with the credentials that are synchronized there. Previously only the desktop gateway knew these services, so a remote workspace received the credentials but its gateway could not match a request to the service and never injected them.

Signing in to Claude no longer needs hand-written credentials: `claude.ai` is registered with a browser sign-in that opens the claude.ai login page and captures the `sessionKey` session cookie, so the connectors page and the permission dialog can offer it like any other service.

A custom service's registration is also kept up to date now. It used to be written once, when the service was first seen, so a definition that changed in a later release (a new base URL, or a newly-added sign-in like the one above) never reached installs that already carried the old one.

Custom services (the ones minds ships itself, currently `claude.ai`) now describe their latchkey registration in latchkey's own format, and minds copies it into latchkey's config without interpreting it.

Previously the bundled definition used its own field names and Python models translated them into latchkey's shape. Two model classes and the translation step are gone: latchkey owns that schema and validates it when it loads the config, so registering a new custom service — or picking up a registration field a later latchkey release adds — is now a data-only change.

No behavior change: the config written for `claude.ai` is byte-for-byte what it was.

## 2026-08-13

Bump Latchkey to 3.5.0.

Failed `latchkey auth ...` invocations now return a condensed, user-facing error detail instead of the CLI's raw output. A crashed latchkey CLI prints a full Node.js uncaught-exception dump (internal frames, a Playwright "Call log:", the stack); `summarize_latchkey_failure` keeps only the meaningful error line (preferring an explicit `Error: ...` line), capped at a sentence-ish length. The full raw output is still logged before summarizing. This is what the minds desktop client shows verbatim in its settings and permission dialogs, which previously rendered the whole stack trace (e.g. the todoist sign-in's "Navigation ... is interrupted by another navigation" crash in latchkey 3.2.2).

`credential_commands.fallback_set_credentials_example()` is the generic `latchkey auth set` invocation to offer for a service that reports no `setCredentialsExample` of its own (a bearer-token header, which `auth set` accepts for any service). It was minds' private fallback in the permission dialog; it lives here now so every caller that turns a service's credential command into a form asks for the same thing.

`Latchkey.services_info()` now returns `None` when the probe fails (process error, malformed output) instead of a degraded sentinel object, so `LatchkeyServiceInfo` is always a faithful parse of latchkey's own output and every caller decides explicitly what a failed probe means for it.

## 2026-08-12

Drop latchkey's built-in `notion` service from the bundled `services.json` catalog.

`notion` was already hidden from agents via `settings.hideBuiltinServices`, but it still shipped in the permission catalog, so it kept showing up in catalog-driven surfaces (the connectors page, the permission dialog, the onboarding carousel) as a service whose credentials latchkey never injects. It is now absent from the catalog, leaving only the separate `notion-mcp` integration.

`scripts/generate_services_json.py` skips every service in `core.HIDDEN_BUILTIN_SERVICES` when regenerating the catalog from detent's schemas, so the two cannot drift apart.

## 2026-08-11

No behaviour change. `maybe_recover_host_permissions_for_agent` documented its unconditional `register_agent_for_host` call as closing a "de-dup gap" in minds' discovery-time auto-register -- which used to treat a missing host permissions file as a terminal verdict and never look at that agent again. Minds now defers such an agent and retries it once the file appears, so the surviving reason this repair re-registers is narrower: the auto-register waits for a host file but never creates one, so an agent on a host whose file was never materialized stays out of the `minds-api-proxy` allowlist until this function materializes it.

## 2026-08-08

Fixed a crash-inducing race in `save_permissions`: it wrote to a fixed `latchkey_permissions.json.tmp` name before `os.replace`, so two concurrent writers (e.g. the minds auto-register callback firing from both the startup thread and the discovery thread) could steal each other's temp file, raising `FileNotFoundError`. Now uses the shared `atomic_write` helper, which writes to a uniquely-named temp file.

## 2026-08-05

Bump Latchkey to 3.4.0.

New `imbue.mngr_latchkey.credential_commands` module: it parses the
`setCredentialsExample` a service reports (e.g. `latchkey auth set-nocurl aws
<access-key-id> <secret-access-key>`) into argv tokens plus one named parameter
per `<placeholder>`, and builds a runnable argv from a caller-supplied value per
parameter, pinned to one latchkey account. This lets an embedder collect
credentials in its own UI instead of telling the user to run a command in a
terminal (which minds now does for services with no browser sign-in).

`Latchkey.auth_set_credentials()` runs such a command. The argv carries the
user's secrets, so it is passed to the subprocess as a list (never a shell
string) and never logged: every `latchkey auth ...` invocation now also passes a
log-safe process name so the argv cannot leak into logs or errors. Unlike the
browser flows, which wait on a human and stay untimed, storing credentials is
bounded by a 60s timeout.

`LatchkeyServiceInfo` gained an `is_browser_auth_supported` computed field --
true when latchkey advertises a `browser` auth option, or reports no auth
options at all (in which case we don't know and keep offering the browser
flow). It replaces the same check open-coded in minds.

`credential_commands.describe_credential_command_failure()` reduces a rejected
credential command's output to the part worth showing a user: it keeps the
service's explanation of what is wrong with the value and drops the usage lines
("Example: ...", "Usage: ..."), JS stack frames and the redundant "Error:"
prefix around it, capping what is left at 300 characters.

## 2026-08-04

Remote workspaces now expose only their VPS-resident Latchkey gateway at the same `http://127.0.0.1:1989` URL used by local workspaces. Minds-owned extension routes are forwarded from the VPS gateway to the desktop gateway, preserving permissions, approval, and Minds API behavior without a secondary gateway environment variable.

VPS-backed workspace requests now use the gateway's synchronized default permissions file instead of copying desktop-only opaque JWT targets onto the VPS. Workspaces created before this change still carry a permissions-override JWT in their host environment, so provisioning temporarily symlinks that JWT's target path at the VPS permissions file; otherwise the remote gateway would reject every request from those workspaces with HTTP 400. The forwarding extension replaces the override header with a dedicated desktop-target JWT for desktop-owned routes, obsolete port-1990 tunnels are removed during provisioning, and the desktop-to-VPS tunnel is torn down with its workspace agent.

The forward supervisor now refreshes its own provider set on `SIGHUP` (previously only its `mngr observe` child did), so a workspace created on a provider that was registered after the supervisor started is no longer left without a VPS gateway until the app is restarted. A workspace that is missing from a provider's cached host listing (the supervisor holds one long-lived provider instance, and some providers cache their whole listing on it with no expiry) now refreshes that listing and looks again, instead of concluding the host has no VPS. A gateway route that cannot be resolved yet is also no longer cached as "use the desktop gateway". Such a workspace now gets no gateway wired at all (with a warning naming the provider), rather than being tunneled to the desktop gateway as a guess: that guess only half-worked for a VPS-backed workspace (it has no permissions override, so everything except the gateway's unchecked RPC endpoint was denied), it exposed the desktop gateway to a workspace not entitled to it, and it occupied the in-container port the VPS tunnel needs.

## 2026-08-03

Remove the `minds-workspaces-sharing` workspace permission verb: sharing moved from a per-service, gateway-proxied workspace sub-resource to a machine-level desktop-only API, so it no longer needs a per-workspace latchkey grant.

Merge main into the self-hosted sharing branch (no further `mngr_latchkey` changes beyond the sibling entries in this PR).

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

Drop the workspace-sharing verb from workspace permissions: share-visitor authorization moved to the relay/broker path (grants enforced workspace-side), so latchkey no longer models it.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-31

Bump Latchkey to 3.3.0: both the minimum version accepted from a locally installed CLI (`LATCHKEY_MIN_VERSION`) and the version installed on remote VPS hosts (`LATCHKEY_VERSION`). Raising the floor forces CLI-only users onto 3.3.0; it moves purely to stay in lockstep with what gets installed and bundled, since the last release this package has a hard code dependency on is still 3.2.0.

- The VPS (outer-host) latchkey gateway now installs datalib v0.26.0's curl release (was v0.25.0), and fetches the statically linked musl build rather than the glibc one. The glibc build only runs where glibc is at least as new as datalib's build host; the musl build has no such requirement, so the Chrome-impersonating curl works on any Linux VPS image.

- Provisioning now replaces an out-of-date curl pair instead of skipping the install whenever the binaries merely exist. An already-provisioned VPS therefore picks up this (and any future) curl bump on its next provisioning pass -- previously it kept the release it was first provisioned with forever, which is exactly the case the musl switch needed to reach.

- The replacement is done by staging both binaries and renaming them into place, so a bump applied to a live VPS cannot fail with "Text file busy" when the gateway happens to be running a request through the curl being replaced, and an interrupted install leaves the previous pair intact rather than a new dispatch curl fronting an old impersonator.

## 2026-07-29

The raw stdout/stderr capture files written when a detached child is spawned (`latchkey_forward.log`, `latchkey_ensure_browser.log`) are now dated and size-bounded.

Each spawn appends a `<timestamp> === spawning ... ===` marker before handing the file descriptor to the child. These files hold the startup-failure output (Click errors, a pre-logging traceback) that never reaches the structured `events.jsonl`, and previously carried no times at all, so there was no way to tell which run produced a traceback or to line it up against the timestamped logs uploaded with it. The child's own lines still cannot be stamped from the parent -- it writes to the descriptor directly, and putting a pipe in between would break the detachment these spawns exist to provide -- so the marker dates the boundary instead.

An oversized capture is now rotated to `<name>.<timestamp>` at spawn time (10MB threshold, keeping only the newest rotation). Spawn time is the only safe moment to do this, since no child holds the descriptor yet. These files are append-only for the life of an install, so one left large by an older build -- or by a child crash-looping before its logging is configured -- would otherwise stay large forever, and be gzipped and re-uploaded with every bug report.

Added `minds-app-version-read` to the agent permissions baseline: every agent may `GET /minds-api-proxy/api/v1/app/version` (the newest workspace-template ref the minds desktop app supports) without a grant. Like the existing `minds-api-schema-read`, it is a permission on the domain-only `latchkey-self` scope, pinned by `const` to that exact method and path. A workspace's `update-self` flow reads this route unattended, from a background worker, to cap how far it may upgrade -- gating it behind a dialog would mean either blocking on a prompt nobody is there to answer or updating past the app.

The grant pins the `/app/version` subpath rather than `/app` so that an ungated, dialog-free permission cannot later widen to whatever app state a new route hangs off that prefix.

Added `reconcile_baseline_permissions`, applied by `register_agent_for_host`, so a *new* baseline permission reaches hosts that already exist. The baseline is only written when a host's permissions file is first created, so previously a file written by an older build kept that build's set of gateway-self grants forever. Registration already reads and rewrites that exact file, so reconciling there costs nothing extra and spares a future baseline *permission* its own migration. It only ever *adds* to the `latchkey-self` rule, and nothing can revoke a baseline permission, so it cannot resurrect something a user turned off. An already-registered agent no longer returns early without writing, which is what would otherwise strand the new grant on exactly the hosts that need it.

Two limits are worth knowing. Reconciliation reaches a host the first time that host's agents are registered after an app restart -- registration de-duplicates per host/agent pair for the process lifetime -- so a workspace that never comes back is never healed. And it only adds permissions to an existing gateway-self rule; a new rule or scope still needs a migration.

The same pass also heals the shared additional-services `include` when it is missing, on every write path rather than only when a brand-new agent registers. Without it, a host that lost the include references an unresolvable schema, and detent then fails that host's whole permission check rather than just the one rule.

Fixed unbounded memory growth in `mngr latchkey forward`. It supervises a `mngr observe --discovery-only` child that emits a discovery snapshot per provider every 30 seconds for as long as the forward runs, and the forward retained every one of those lines for its entire lifetime. On a machine left running for a week or two this accumulated well over a gigabyte (visible on macOS as a large "Memory" / "VM Compressed" figure in Activity Monitor, with a small resident size, because the leaked pages are never touched again and so get compressed).

The discovery stream is now started with `is_output_accumulated=False`, so lines are dispatched to the discovery handlers as they arrive and then dropped. The child's stderr continues to be logged.

- The VPS (outer-host) latchkey gateway now installs datalib v0.25.0's curl release (was v0.22.0), in which Chrome TLS impersonation actually engages for proxied requests. Previously the marker header that selects the impersonating curl could not survive the hop to the gateway (it was value-less, which is curl's syntax for *removing* a header and so has no representation on the wire), the dispatch curl matched only one exact spelling of it, and the impersonator let the gateway's forwarded `User-Agent: curl/...` override Chrome's. Requests through the gateway consequently never got impersonation, whatever they asked for.

## 2026-07-28

Upgraded to Latchkey 3.2.0 (Detent 1.11.2) and made third-party service permissions **per account** instead of per service.

- Changed: a grant for a catalog service is now a rule keyed `<scope>:<account>` (e.g. `slack-api:hynek@imbue-ai`; the unnamed default account is the empty string), backed by a generated schema that intersects the built-in scope with the account Latchkey reports for the request. Two accounts of the same service therefore hold independent permissions. The key is only a human-readable identifier and is never parsed -- a scope or an account may legitimately contain a colon -- so everything that needs to know what a rule grants inspects the schema structure instead. It is still unique per (scope, account) pair: the scope half is percent-escaped before the two are joined, which is the identity for every scope name the catalog ships. The new `imbue.mngr_latchkey.account_scopes` module owns both sides of that structure (`build_account_grant` to write one, `list_account_grants` / `resolve_account_scope` / `resolved_schema_names` to read them back), and `ServicesCatalog.list_service_account_grants` is the single entry point consumers use to turn a permissions file into (service, account, permissions) triples. The gateway's `permission_requests` extension keeps a drift-guarded JavaScript copy of the two generating helpers.

- Changed: `permission-requests` moved to on-disk schema `v3`, so no legacy pending requests are carried over. A `predefined` request payload now takes an optional `account`; a request that names none has an empty `effect` and can only be granted by a client that supplies the chosen account in the approve override body (`{"account": ..., "permissions": [...]}`, newly accepted for `predefined` requests).

- Changed: `POST /permissions/rules` now takes `{"permissions": [...], "schemas": {...}}` instead of a bare array of permission names, and merges the supplied schema definitions into the file by name. The extension synthesizes no schemas and never interprets the rule key, so a caller whose key is not a name Detent already knows must define it in the request.

- Added: data-format migration 2 rewrites every per-host `latchkey_permissions.json` into the per-account shape -- one rule per account currently stored for the service, dropping grants for services with no stored account (nothing could be injected under them). Minds' own gateway-self scopes are untouched. A file that cannot be read, parsed, or rewritten is reset to the permissions a freshly-created host gets rather than failing the run. The migration reads the stored accounts itself (`latchkey auth list --offline`), only once it has a file to rewrite, and aborts rather than guessing if that listing fails; migrations are now handed the latchkey directory and binary alongside the plugin data directory for exactly this kind of lookup.

- Changed: `services_for_permissions` resolves which service a rule needs by walking the rule key's schema `$ref` closure rather than by reading its name, so a per-account grant still ships the right credentials to a VPS.

- Changed: `Latchkey.auth_browser` / `auth_browser_login` accept an `account` to re-authenticate a specific stored account.

- Changed: the minimum (and VPS-installed) Latchkey version is now 3.2.0, the first release that reports the injected account to Detent.

Support for *additional* (custom) latchkey services -- third-party services minds supports that detent has no builtin schemas for. They are hand-maintained in `additional_services.json`, seeded with `claude.ai` (one `everything` permission granting full access to the `claude.ai` domain).

Their catalog entries are folded into the generated `services.json` by `scripts/generate_services_json.py`, so every reader -- the desktop dialog's `services_catalog.ServicesCatalog` and both gateway extensions -- works from one file in one shape and needs no additional-service parsing of its own. `additional_services.json` remains the source of the two things the catalog does not carry: each service's `base_api_url` (for registration) and its inline detent schemas (for the shared include file).

Because a custom scope is not a detent builtin schema, its scope/permission schemas are materialized **once** into a shared `minds_shared_schemas.json` (a schemas-only detent config) that every per-host `latchkey_permissions.json` references via detent's `include` directive. The include is a bare relative name, so it resolves next to the referencing file on both the desktop (the gateway's opaque-handle directory) and a VPS (`~/.latchkey`, where `remote_gateway` ships the shared file alongside the permissions file). Granting a custom scope is therefore a plain rule write with no per-host schema inlining. `LatchkeyPermissionsConfig` gained an `include` field; the agent baseline carries the include for new hosts and a new data-format migration (version 3) stamps it into pre-existing host files.

`Latchkey.initialize()` now materializes the shared schemas file and registers each additional service with the latchkey CLI (`latchkey services register <name> --base-api-url <url>`) so latchkey can inject the user's credentials for the service's domain. Registration happens before the gateway is spawned and is best-effort; because `services register` is not idempotent (it errors when the service already exists), existing services are skipped via a `latchkey services list` pre-check.

`register_agent_for_host` now copy-updates the existing permissions config instead of rebuilding it from `rules`/`schemas`, so registering an agent on a host no longer drops the file's `include` (which would leave an already-granted custom scope referencing a schema detent can no longer resolve). It also idempotently *ensures* the baseline's includes are present, so a host file that already lost its include is repaired on the next agent registration -- the data-format migration cannot do this, since it only runs when the recorded version changes.

## 2026-07-27

Every agent's baseline latchkey permissions now include `minds-api-timezone-read`: a read-only grant for `GET /minds-api-proxy/api/v1/timezone`, mirroring the existing API-schema read grant. Workspace agents can ask the minds desktop client for the user's current IANA timezone (e.g. to run scheduled jobs in the user's local time) without a per-agent permission dialog.

## 2026-07-25

Hardened the upgrade path onto latchkey 3.x's multiple-accounts credential-store format for installs whose stores were written by latchkey 2.x (everything created before minds-v0.3.9). The upstream CLI migrates the store automatically at its next invocation, but three things around that migration could break or corrupt state:

- The `latchkey --version` timeout in `Latchkey.initialize()` is raised from 5s to 15s. The upstream CLI runs its store migrations before printing anything -- including `--version` -- and the 3.x migration makes per-service network calls on the first invocation after an upgrade, so the old 5s budget could kill it mid-migration.

- The VPS provisioning script's latchkey version probe now reads the npm-installed package's `package.json` instead of executing `latchkey --version`. The CLI resolves its encryption key before printing anything; on a headless VPS with an existing credential store and no key in the environment that exits non-zero, which read as a permanent version mismatch and reinstalled latchkey on every provisioning pass. The probe fix also makes the new restart-on-upgrade behavior fire only when the version actually changed: after an `npm install -g latchkey@<pinned>`, the supervisord-managed VPS gateway is restarted, since `supervisorctl reread`/`update` only bounce a program whose config changed and would otherwise leave the old code running in memory against a store it can no longer read.

- `sync_credentials` now ships the desktop's upstream `data-format-version` stamp to the VPS, written before the store itself. Previously only `credentials.json.enc` was synced, so a VPS stamped by the old latchkey would "migrate" the already-current synced store at its next gateway start and corrupt its copy of the credentials; stamp-first ordering keeps an interrupted sync on the benign side.

## 2026-07-24

The `mngr latchkey forward` daemon's Sentry consent file now carries a single flag.

`ForwardSentryConsent` (the JSON the embedder writes to `MNGR_LATCHKEY_SENTRY_CONSENT_FILE`) drops the `include_error_logs` field: the remaining `report_unexpected_errors` flag now gates both whether the daemon sends automatic reports and whether their log/traceback attachments are uploaded. An absent/unreadable consent file still means reporting is off.

`LatchkeyForwardSupervisor.stop()` now terminates the supervisor's descendant subprocesses (its `mngr observe` discovery child, the detached `latchkey gateway`, reverse `ssh` tunnels) as well as the supervisor itself, the same way the duplicate-forward reaper already did. Previously a wedged supervisor that had to be SIGKILLed after the grace period never ran its own teardown, and the gateway -- spawned detached so it survives desktop-client restarts -- would outlive every session and accumulate as an orphan across app restarts.

Hide latchkey's built-in `notion` service from agents.

The gateway now merges a `settings.hideBuiltinServices` list (currently just `notion`) into `LATCHKEY_DIRECTORY/config.json` before each spawn, so agents no longer see the built-in `notion` service alongside the separate `notion-mcp` integration (which was a source of confusion). Existing config content is preserved via a read-merge-write.

The same hidden set is applied to VPS-resident gateways provisioned by `provision_remote_gateway`, so agents talking to a per-VPS gateway see the same service list as those talking to the desktop gateway.

- The VPS (outer-host) latchkey gateway now supports on-demand Chrome impersonation. Its install step best-effort fetches the "dispatch curl" and the Chrome-TLS-impersonating curl from the datalib release, and the gateway run script points `LATCHKEY_CURL` at the dispatch curl when both are present. Requests carrying the marker header `X-Imbue-Impersonate:` get impersonation; all other requests pass through to the system curl.

- The curl install is deliberately non-fatal: a fetch failure (e.g. a datalib release that predates the curl tarball) never breaks provisioning, and the run script guards the `LATCHKEY_CURL` export on the binaries' presence, so a host without them falls back to the system curl unchanged.

Fixed a startup race in which an early `LatchkeyForwardSupervisor.bounce()` could silently kill a freshly-spawned `mngr latchkey forward` supervisor: the forward publishes its pid record well before it installs its SIGHUP handler, so a bounce landing in that window terminated it via the signal's default disposition (observed in practice from the minds discovery-health watchdog sampling mid-startup).

- `mngr latchkey forward` now ignores SIGHUP for its whole startup window (from process start until the real bounce handler is installed), so an early bounce is dropped instead of fatal.

- `LatchkeyForwardSupervisor.bounce()` now skips the SIGHUP entirely while the forward's record carries no gateway port (i.e. startup has not finished): the `mngr observe` child it would bounce does not exist yet, and startup reads the current provider state anyway.

## 2026-07-23

Support for latchkey 3.0.0's multiple-accounts-per-service model.

`Latchkey.services_info` now parses latchkey 3.0.0's per-account `credentials` object (keyed by account name, with the unnamed default account keyed by `""`) instead of the removed top-level `credentialStatus` field. The parsed accounts are exposed on `LatchkeyServiceInfo.accounts`, and a single service-level `credential_status` is derived from them (no accounts means `MISSING`; any valid account means `VALID`; otherwise `UNKNOWN` before `INVALID`), so the existing permission-grant flow keeps working unchanged.

New `Latchkey.auth_list(is_offline=...)` runs `latchkey auth list [--offline]` once and returns every service's stored accounts keyed by service name, so callers can enumerate accounts without a per-service `services_info` call.

`Latchkey.auth_clear` is now account-aware: `auth_clear(service, account=...)` clears one account and `auth_clear(service, is_all=True)` wipes every account and the prepared OAuth client (`--all`, required by 3.0.0 to also drop preparations).

New `Latchkey.add_account(service)` runs the browser sign-in with `LATCHKEY_EPHEMERAL_BROWSER=1` so the user lands on a fresh sign-in screen and can add a genuinely new account; for Google services it always falls back to a fresh `auth browser-prepare` when signing in with the official Minds client does not succeed.

Landed the latchkey 3.x multiple-accounts support (from `hynek/multiple-accounts`): per-account credential parsing in `services_info`, the new `auth_list` and `add_account` methods, an account-aware `auth_clear`, and the `LATCHKEY_MIN_VERSION` bump to 3.1.0. See the `hynek-multiple-accounts` entry for the full description.

## 2026-07-17

Bump Latchkey to v2.21.0.

## 2026-07-16

Fixed: `mngr latchkey forward` no longer treats a stopped (but not destroyed) workspace as if it were still running. Previously, when a workspace's container was stopped -- via an app restart, an idle-shutdown, or a VPS reboot -- discovery kept reporting the agent, so the supervisor would repeatedly try to provision the VPS-resident gateway against the stopped container (raising `container ... is not running`) and would keep the agent's reverse tunnel alive, leaving the tunnel health-check loop re-dialing a dead endpoint indefinitely.

The discovery stream now carries each host's lifecycle state through to the discovery handler. When a host is reported as not-running, the handler tears down that agent's reverse tunnel and skips VPS gateway provisioning (the shared desktop gateway still stays up, since it is shared across all agents). When the host returns to running, discovery re-fires and both the tunnel and the VPS gateway are re-established.

VPS latchkey provisioning now gates the NodeSource Node.js install behind a version check instead of a presence check. Previously, a VPS with a preinstalled but too-old node (e.g. Debian bookworm's distro nodejs 18.x) skipped the install and then crashed cryptically in `npm install -g latchkey` (modern npm refuses node < 20.17). Any node older than major 20 is now replaced with the pinned NodeSource install, and if a stale node still shadows the fresh one on PATH afterwards (e.g. a manual /usr/local/bin/node), provisioning fails with an actionable message naming the shadowing binary instead of the cryptic npm crash.

Found by the new opt-in latchkey remote-workspace e2e release test (`apps/minds/test_latchkey_e2e.py`) running against a Debian host with node 18 preinstalled.

## 2026-07-15

The `mngr latchkey forward` daemon now attaches the install's anonymous user id (no PII) to its Sentry events, so its reports count as the same install as the minds backend's in Sentry's per-issue user counts. The id is inherited from the embedder via the new `MNGR_LATCHKEY_SENTRY_USER_ID` environment variable (required alongside the other `MNGR_LATCHKEY_SENTRY_*` infrastructure vars).

- Changed: the `mngr latchkey forward` supervisor now caps its dedicated rotated `events.jsonl` log at 10MB (down from the general mngr default of 100MB), matching the 10MB cap the minds desktop client uses for its own rotated logs. Older rotated copies are still pruned to the newest 10.

## 2026-07-14

Update Latchkey to include support for GitHub's GraphQL API.

Fixed an endless remote-sync feedback loop in the `mngr latchkey forward` supervisor's remote-state watcher.

The watchdog handler that keeps a remote (VPS) host's latchkey credentials and permissions in sync reacted to *every* filesystem event on the watched files. On Linux, watchdog's inotify observer also dispatches read-lifecycle events (`FileOpenedEvent` / `FileClosedNoWriteEvent`) whenever a watched file is merely read -- and the sync itself reads those files (`sync_permissions` reads the host permissions file; `sync_credentials` re-reads it and spawns latchkey CLI subprocesses that open the credentials store). Each sync therefore re-triggered the next one, producing a full VPS re-sync (SSH upload plus a `latchkey auth re-encrypt` subprocess) roughly every 6 seconds for the supervisor's entire lifetime, even though neither file was ever modified.

`_LatchkeyStateChangeHandler.dispatch` now allowlists genuine mutation events only (`FileCreatedEvent`, `FileDeletedEvent`, `FileModifiedEvent`, `FileMovedEvent`), so reads -- including the sync's own -- are inert. `FileClosedEvent` (IN_CLOSE_WRITE) is also excluded since a content-changing write always emits `FileModifiedEvent` as well, and reacting to both would double-fire syncs.

A change to a host's permissions file now syncs that host's full latchkey state (permissions, then credentials) to the VPS instead of permissions only. The permissions determine which services' credentials ship to the host, so a grant must deliver the newly-allowed service's credentials and a revocation must remove the no-longer-allowed ones -- previously the VPS credential store stayed stale until the next supervisor restart or a separate credentials-file change.

## 2026-07-13

The `mngr latchkey forward` daemon now tells Sentry to ignore the paramiko/pyinfra stdlib loggers. The daemon reverse-tunnels the shared gateway into every agent via paramiko; when a target went offline and the health check retried, paramiko's transport thread logged the connection-reset failure at ERROR level, and Sentry's default logging integration captured each one as an event. Those events were not even rate-limited (the stdlib records carry no exception info or fingerprint), so a handful of users produced tens of thousands of Sentry events. The daemon now drops that already-handled noise while still reporting genuine failures raised through loguru.

Fixed the VPS-resident ("secondary") latchkey gateway being unreachable from the agent's container on lima-slice hosts, which left agents with no latchkey access whenever the desktop (primary) gateway was unavailable -- e.g. while the user's laptop was offline.

The VPS->container reverse tunnel was opened to the container's externally-routable SSH port instead of the port the container's sshd is published on from the outer host's own loopback. On a slice (where those two ports differ) the tunnel was refused, so the secondary gateway never bound inside the container. The reverse tunnel now uses the outer-host-loopback port supplied by the provider.

Tunnel setup is also no longer silently treated as successful when the `ssh -R` process dies immediately (e.g. connection refused or a failed forward bind): after backgrounding it, the launch now confirms the process survives a short window, so a broken tunnel surfaces as a failed provision and is retried instead of being cached as "provisioned".

The VPS-resident Latchkey gateway and its VPS->container reverse SSH tunnel are now supervised by `supervisord` instead of being spawned detached (`nohup` + a PID-file guard). `supervisord` is installed from the distro package during remote-gateway provisioning; both processes are registered as `autostart`/`autorestart` programs, so a crashed gateway or tunnel is restarted automatically without a desktop round-trip.

A VPS previously provisioned by an older build (which launched the gateway and tunnel detached via `nohup`, tracked by `~/.latchkey/{gateway,tunnel}.pid`) is migrated automatically: provisioning kills those legacy processes (freeing the gateway port and the container forward bind) and removes any encryption key/password an intermediate build had persisted to disk, before the `supervisord` programs start. Without this the new programs would fail to start against the still-running old ones.

The reverse SSH tunnel now carries keepalive and timeout flags (`ServerAliveInterval`, `ServerAliveCountMax`, `TCPKeepAlive`, `ConnectTimeout`, `BatchMode`) so a connection wedged by a paused-then-resumed VM is detected and torn down, letting `supervisord` re-establish a fresh tunnel.

The gateway's secrets (the encryption key and derived listen password) are kept in a RAM-backed tmpfs directory under `/run` rather than on the persistent disk: this keeps the encryption key off disk (a key file beside the encrypted credential store would be equivalent to storing the credentials in plaintext against a disk-snapshot threat) while still surviving process crashes so `supervisord` can restart the gateway. Provisioning verifies the directory is genuinely RAM-backed (tmpfs/ramfs) before writing the key and refuses to proceed otherwise, so the key can never be silently written to a disk-backed filesystem. The tradeoff is that a full reboot wipes the secrets, leaving the gateway down until the next provisioning pass re-writes them (crash recovery is supported; reboot recovery is intentionally not).

`mngr latchkey forward` now supervises the shared `latchkey gateway` subprocess: a background health check detects when the gateway has died mid-session and respawns it on its original port (so agent reverse tunnels and the published gateway port stay valid across the restart). Previously a crashed gateway went unnoticed -- discovery and reverse tunnels stayed up, so nothing restarted it -- and agents could no longer reach the gateway until the whole app was restarted.

`Latchkey.is_gateway_running` and `Latchkey.start_gateway` are now liveness-aware: they check the gateway subprocess's actual status (via `poll()`) rather than merely whether a record is tracked, so a dead gateway reads as not-running and is respawned instead of returning a stale port.

## 2026-07-10

The latchkey discovery stream consumer no longer logs a "Discovery error from ..." warning on every poll cycle for a provider stuck on the same failure (e.g. missing credentials): provider-level discovery errors are now logged once per process via the shared `DiscoveryErrorLogSuppressor`, with an info-level recovery line (and re-armed suppression) when the provider's discovery next succeeds. Host- and agent-attributed discovery errors keep logging on every occurrence.

Both latchkey gateway spawn sites (the shared desktop gateway and the
VPS-resident gateway) now pass `--max-body-size` raised to 512 MiB (upstream
default: 10 MiB).

The gateway natively proxies GitHub's git smart-HTTP endpoints
(`/gateway/https://github.com/<owner>/<repo>.git/...`, gated by the
`github-git` scope with `github-git-read`/`github-git-write` permissions), so
agents can run `git push`/`git fetch` through it with the credential injected
server-side -- but a push's packfile scales with repo history (a minds
template push is roughly 30 MiB today), which exceeded the old 10 MiB cap and
made gateway-authenticated pushes fail. The forever-claude-template's
publish-inspiration flow now relies on this push path.

Already-running gateways keep the old cap until they restart: the desktop
gateway picks the flag up on the next minds-app session, and a VPS gateway on
the next launch after its recorded process exits (the pidfile guard
deliberately never kills a live gateway).

## 2026-07-09

# Cover the new backup disable route with the backups-manage verb

- The `minds-workspaces-backups-manage` target-scoped verb's path pattern now also covers `POST /api/v1/workspaces/<id>/backup-service/disable` (the minds app's new "turn backups off" action), and its description mentions disabling.

Added a new target-scoped verb to the `minds-workspaces` permission scope: `minds-workspaces-backups-manage`, gating the new backup-management routes (`/backup-service/update`, `/backup-service/update/cancel`, `/backup-service/configure`, `/backup-service/verification`) so an agent needs an explicit per-target grant to update a workspace's backup service, enable/repoint its backups, or toggle backup verification.

## 2026-07-08

Add `SECRET_LATCHKEY_ENV_VAR_NAMES` to `agent_setup`: the subset of latchkey wiring env vars whose values are secrets (the gateway password and the permissions-override JWT). Callers that render a command carrying these as `--host-env NAME=VALUE` flags use it to mask the values before logging.

## 2026-07-07

Bump Latchkey to 2.20.0.

## 2026-07-06

Simplifies how cross-workspace (`minds-workspaces`) permission grants are stored, and removes the rule-ordering fragility that a granted grant depended on.

Previously the cross-workspace management API was given its own `minds-workspaces` detent scope. Because that scope and the agent baseline's `latchkey-self` scope both match on the gateway-self domain alone, two same-domain rules ended up in a host's `latchkey_permissions.json`, and detent's first-matching-scope-wins evaluation made the rule *order* load-bearing (a domain-only catch-all placed first would shadow and veto the narrower grant). The cross-workspace verbs now attach as permissions on the single `latchkey-self` scope instead -- exactly like file-sharing and accounts -- so there is only ever one gateway-self rule and rule order no longer affects whether a grant takes effect. The order-normalizing machinery added to keep the catch-all last (in both the gateway's approve merge and `register_agent_for_host`) is gone.

Because the cross-workspace verbs no longer sit under a dedicated detent scope, the `MINDS_WORKSPACES_SCOPE` constant is gone from `workspace_permissions.py`, and the now-unused `scope` and `gateway_self_host` fields are dropped from the shared `workspace_permissions.json` catalog (only `path_prefix` and `verbs` are still consumed). The migration that reconstructs the historical two-scope layout carries its own frozen copies of the legacy scope name, gateway-self host, and path prefix, so it cannot drift with the live verb catalog.

Adds a small, reversible data-format migration mechanism. The plugin records the on-disk data-format version in a `data-format-version` file at the root of its data directory (`<latchkey_directory>/mngr_latchkey/`); a `migrations` package holds one `up`/`down` migration per version. On `Latchkey.initialize()` the recorded version is reconciled against the version the installed code targets, applying the intervening migrations in the correct direction and re-stamping the file. The first migration folds any existing host file's `minds-workspaces` rule into its `latchkey-self` rule (and drops the now-defunct scope schema), so grants written by older builds keep working after an upgrade; its `down` reverses the fold. To keep it a stable historical artifact, that migration depends on no live plugin code: it carries its own frozen copies of the legacy scope constants, permissions-file model, on-disk layout, and read/write helpers, so it keeps performing the same rewrite even if the store model, its serialization, or the directory layout later change.

`mngr latchkey forward` (the long-running gateway/reverse-tunnel supervisor daemon) now reports errors to Sentry, using the shared error-reporting machinery in `imbue_common`. It reports to whichever Sentry project the embedder points it at, tagged with the `mngr-latchkey-forward` service name so its events are distinguishable, and attaches its own structured (`events.jsonl`) and raw (`latchkey_forward.log`) logs.

Reporting is off by default and configured entirely via `MNGR_LATCHKEY_SENTRY_*` environment variables (deliberately namespaced `MNGR_LATCHKEY_*`, not `LATCHKEY_*`, to distinguish `mngr latchkey` from the upstream core `latchkey` project). The daemon owns no Sentry project / environment definitions of its own -- it receives concrete values as strings. The (mostly static) infrastructure is snapshotted into its environment at spawn:

- `MNGR_LATCHKEY_SENTRY_DSN` -- the Sentry DSN to report to.

- `MNGR_LATCHKEY_SENTRY_ENVIRONMENT` -- the Sentry environment label.

- `MNGR_LATCHKEY_SENTRY_RELEASE` and `MNGR_LATCHKEY_SENTRY_GIT_SHA` -- the release version and git SHA events are tagged with.

- `MNGR_LATCHKEY_SENTRY_S3_BUCKET` -- the S3 bucket for log/traceback attachments; empty means there is no bucket, so nothing is uploaded.

Sentry initializes whenever `DSN`, `ENVIRONMENT`, `RELEASE`, and `GIT_SHA` are all present (the daemon has no fallback of its own; run standalone without them it does nothing).

Whether the daemon actually *sends* reports (and attaches logs) is gated by a live consent file at `MNGR_LATCHKEY_SENTRY_CONSENT_FILE`, read on every event. This lets the embedder toggle consent on a running daemon -- a grant/revoke takes effect immediately, with no respawn -- exactly mirroring how the minds backend gates its own Sentry on live user settings.

When the minds desktop client spawns the daemon, it publishes the infrastructure variables automatically (resolving the DSN / environment / bucket from its own Sentry settings) and maintains the consent file from the user's error-reporting settings, rewriting it whenever the user changes consent.

Unhandled exceptions that escape the supervisor are now logged through loguru before the process exits, so their Sentry reports carry the daemon's logs + traceback as attachments (an exception captured directly by the SDK's excepthook integration would otherwise bypass that handler and arrive with no logs). Expected `click` control-flow exits are left as-is and not reported as errors.

Migrated the `mngr latchkey forward` discovery consumer to the per-provider discovery model. It now folds each `DISCOVERY_PROVIDER` snapshot and every incremental discovery event into the shared `DiscoveryStateAggregator` instead of hand-rolling its own prior-vs-fresh reconciliation. A snapshot is authoritative only for its own provider, so one provider's poll no longer affects another provider's agents, and retain-on-provider-error is handled inside the aggregator. Legacy global `DISCOVERY_FULL` snapshot handling has been removed.

Fixed a span-awareness gap: a per-provider snapshot no longer re-establishes a reverse tunnel for an agent that was destroyed during that snapshot's discovery span (the snapshot's discovery callbacks are now restricted to agents the aggregator actually kept).

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

## 2026-06-30

The per-agent minds bug-report route (`POST /minds-api-proxy/api/v1/agents/<id>/report`) is now reachable by any in-workspace agent without a prior per-agent permission grant: a baseline rule allows that exact path ahead of the unauthorized gate. The route's bearer-key auth still applies. This is an interim allowance pending broader minds-API-surface latchkey work.

## 2026-06-28

Added a third permission-request type, `workspace`, to the `permission-requests` gateway extension (alongside `predefined` and `file-sharing`), for the cross-workspace `minds-workspaces` API.

- The extension validates the requested verbs against the `minds-workspaces` verb set (read / create / destroy / lifecycle / backups-export / ssh) and the optional `target_workspace_id` as an agent id, and computes a self-contained `effect` (the scope schema + per-verb permission schemas + the grant rule), applied via the standard `POST /permission-requests/approve` path exactly like file-sharing.

- The target-scoped verbs (destroy / lifecycle / backups-export / ssh) mint a uniquely-named per-target schema (`minds-workspaces-<verb>-<target_id>`) for a "selected" grant, or a broad schema for an "all workspaces" grant. Successive grants accumulate targets through the gateway's ordinary schema-by-name merge -- no `anyOf` and no special merge logic.

- The approve override body is extended so a `workspace` request can recompute its effect at approval time from the user's dialog choices (`{permissions, target_workspace_id}`), mirroring file-sharing's `{path}` override.

- The `minds-workspaces` scope is no longer part of the agent baseline and is not in the service catalog: it has its own dedicated request type, and its scope + verb schemas arrive self-described in the grant effect. The startup schema-backfill migration for it has been removed, and the now-empty `_MANUAL_SERVICE_ENTRIES` hook in the `services.json` generation script has been dropped.

- The `minds-workspaces` verb catalog now lives in a single shared data file, `extensions/workspace_permissions.json`, read by both the gateway extension (for request-path schema construction) and the Python `workspace_permissions` module (for dialog-facing verb metadata), so the two sides cannot drift.

Added the `minds-workspaces` detent scope and its named permissions (`minds-workspaces-read`, `-create`, `-destroy`, `-lifecycle`, `-backups-export`, `-ssh`) to the per-agent latchkey permissions baseline. This gates the minds desktop client's new cross-workspace management API (`/minds-api-proxy/api/v1/workspaces/...`): the scope is materialized in every per-host permissions file but not pre-granted, so an agent goes through the standard permission-request dialog before its first cross-workspace call and the user picks which verbs to allow.

- `ensure_minds_workspaces_schema_in_existing_host_files` backfills the scope + permission schemas into permissions files created before the scope shipped (run at `minds run` startup, before the gateway restarts).

- `store.list_host_permissions_paths` enumerates the per-host permissions files (used by the migration).

- The `services.json` generator now preserves manually-curated, non-detent scope entries (like `minds-workspaces`) across regenerations.

- The `minds-workspaces-ssh` permission now pins its HTTP method to `POST`, matching the other write verbs (`-create`/`-destroy`/`-lifecycle`/`-backups-export`) and the scope's one-verb-per-permission convention.

Grants every agent read access to the Minds API schema document by default. The agent permissions baseline now allows `GET /minds-api-proxy/api/schema` (a permission on the existing domain-only `latchkey-self` scope, alongside the existing self-permission reads), so a freshly-created workspace can fetch the OpenAPI description of the gateway-reachable Minds API without any user grant. The schema document is non-agent-scoped (identical for all callers) and read-only.

Fixes a bug where a granted `minds-workspaces` permission (the cross-workspace management API -- list/read/create/destroy/ssh/etc. on other workspaces) was silently rejected with `403` despite being present in the agent's permissions file. The agent baseline's `latchkey-self` scope is *domain-only* -- it matches every gateway-self request on host alone -- and detent evaluates rules in order, treating the first rule whose scope matches as authoritative (it rejects outright if that rule's own permissions do not cover the request, without consulting later rules). Because the workspace grant was appended *after* the `latchkey-self` rule, the catch-all matched first and vetoed the request. The grant rules are now kept ordered with `latchkey-self` last -- both when a grant is approved (the gateway's `permission_requests.mjs` merge) and when an agent is (re)registered (`register_agent_for_host`, which on app restart also repairs a host file written by an older build) -- so a narrower same-domain scope is evaluated first and its grants take effect. Schema and file-sharing access (granted under `latchkey-self` itself) is unaffected.

Adds an `accounts` permission-request type to the gateway's bundled `permission-requests` extension, backing the new must-ask `GET /api/v1/accounts` route on the minds side. Like the existing `file-sharing` type, approving an `accounts` request mints a single fixed permission (`minds-accounts-read`, matching `GET /minds-api-proxy/api/v1/accounts`) under the pre-existing `latchkey-self` scope rather than minting a new detent scope, so it requires no baseline change and is deny-by-default until explicitly granted. The request is all-or-nothing with an empty payload (no verbs, no target).

Adds the `minds-workspaces` detent permission scope used by the minds cross-workspace management API, with one named permission per verb (`minds-workspaces-read`, `-create`, `-destroy`, `-lifecycle`, `-backups-export`, `-ssh`, `-update`, `-recover`, `-sharing`).

- Verbs split on a target axis: `read`/`create` are all-or-nothing, while `destroy`/`lifecycle`/`backups-export`/`ssh`/`update`/`recover`/`sharing` are target-scoped. A "selected" grant mints a uniquely-named per-target permission schema (`minds-workspaces-<verb>-<target_id>`) that pins a single workspace; an "all workspaces" grant uses the broad verb schema. Successive selected grants accumulate targets through the gateway's ordinary schema-by-name merge.

- A verb's `method` in the shared catalog may now be a single HTTP method or an array of methods; multi-method verbs (e.g. `recover` matches `GET`/`POST`, `sharing` matches `GET`/`PUT`/`DELETE`) produce a permission schema whose `method` is a JSON-Schema `enum`.

- The grant is applied like file-sharing: the agent's `type=workspace` permission request carries a precomputed effect (scope schema + verb schemas + rule, built in `permission_requests.mjs`'s `computeWorkspaceEffect`), spliced into the requesting agent's per-host permissions file on approval. The Python side keeps only the dialog-facing verb metadata.

## 2026-06-27

Fixed: a SIGHUP provider refresh (sent whenever a workspace toggles a discovery provider, e.g. disabling OVH) could permanently wedge the `mngr latchkey forward` supervisor's discovery pipeline. The supervisor's `mngr observe` child was tracked as a *checked* concurrency-group strand, so terminating it during a bounce (SIGTERM, a non-zero exit) was re-checked as a failure and raised a `ConcurrencyExceptionGroup`. That group escaped the bounce watcher's narrow `(OSError, RuntimeError)` guard and killed the watcher thread, turning every later SIGHUP bounce into a silent no-op for the rest of that supervisor's life.

The observe stream is now spawned with `is_checked_by_group=False` (it is stopped deliberately on every bounce and on shutdown, so its SIGTERM exit is expected, not a failure), and the SIGHUP bounce watcher now survives any single bounce's error -- it logs and keeps watching instead of dying -- so a later provider toggle can still take effect. `bounce_observe`'s own teardown/respawn guards were likewise widened to cover the concurrency-group failure types they can encounter, and the shutdown `stop()` teardown now tolerates the same `terminate()` failure modes (notably a `TimeoutExpired` force-kill overrun) so a slow-to-die observe child can no longer abort the rest of the forward's clean shutdown.

Stopped duplicate background discovery producers from accumulating, which made the Minds app keep showing errors for providers you had disabled and contributed to a running mind intermittently disappearing (redirecting to the "create a mind" page with "0 minds").

`LatchkeyForwardSupervisor.ensure_running()` previously only reconciled the `mngr latchkey forward` recorded in `latchkey_forward.json`. When that record was missing or stale, it spawned a fresh supervisor without noticing that *other* `mngr latchkey forward` processes (left by a prior or concurrent app instance) were still running against the same latchkey directory. Each of those orphans runs its own `mngr observe --discovery-only` child, so several producers ended up writing the one shared discovery-events file with divergent, frozen-at-startup provider configs: orphans kept polling (and erroring on) providers you had since disabled, and their conflicting snapshots made the consumer flap agents in and out.

`ensure_running()` now enforces one forward per latchkey directory: it reaps every `mngr latchkey forward` bound to the same `--latchkey-directory` (along with that forward's child subprocesses -- its `mngr observe` producer, the `latchkey gateway`, and reverse `ssh` tunnels) except the one that matches the live on-disk record. An unrecorded orphan is always replaced rather than adopted, since it may be running stale code or config. The scan is scoped by resolved `--latchkey-directory` equality, so a supervisor for one profile never signals a sibling profile's forward (e.g. `.minds` vs `.minds-staging`).

Scope: this removes the duplicate/stale-producer source of the problem -- it fully addresses disabled providers continuing to error, and removes the cross-producer snapshot divergence. It does not, on its own, fully fix a mind disappearing: a single discovery producer can still momentarily report a provider as healthy while omitting one of its agents (e.g. the Docker provider intermittently returning no containers without raising), and the consumer currently drops an agent the first time that happens. Hardening that consumer-side retention (and the Docker provider's empty/error behavior) is separate follow-up work.

## 2026-06-26

Bump the pinned latchkey CLI version installed on remote VPS environments (the secondary gateway) to 2.19.1.

`Latchkey.auth_browser` now owns the full Minds Google OAuth flow, so all of the auth logic lives in one place. It attempts the browser sign-in optimistically and, only when latchkey reports that the service has no registered client yet, prefers the Minds-provided OAuth client: for a Minds-OAuth Google service it registers that client (`auth prepare`) and retries against the Minds consent screen before falling back to the user self-setup flow (`auth browser-prepare`). This removes the previous up-front `auth list` probe, which was expensive because it queried every service.

The registered Minds client is cleared (`auth clear`) only when we just registered it and its sign-in then failed; a client that was already registered (for example a user's own) is never cleared.

Add the supporting `Latchkey` primitives used by that flow: `auth_prepare` (register an OAuth client id/secret for a service), `auth_browser_login` (a bare `auth browser` sign-in with no self-setup fallback), and `auth_clear` (`latchkey auth clear -y <service>`). Add an explicit set of Minds-OAuth Google services (`MINDS_GOOGLE_OAUTH_SERVICES`) used to gate the flow -- `google-directions` is deliberately excluded because it authenticates with an API key, not OAuth, so it must not go through the Minds OAuth client -- plus the Minds Google OAuth client id/secret as hardcoded constants (an installed/desktop-app OAuth client, where the "secret" is not truly confidential since it ships inside the distributed client).

Raise the minimum supported latchkey CLI version (`LATCHKEY_MIN_VERSION`) to 2.19.1, kept in lockstep with the version installed/bundled. The package now refuses to operate against gateways older than 2.19.1 (2.18.0 introduced the `auth prepare` subcommand the flow depends on).

## 2026-06-25

Fixed repeated macOS system keychain access dialogs (mentioning Latchkey) that appeared during normal Minds use. The detached `latchkey ensure-browser` subprocess was spawned without `LATCHKEY_ENCRYPTION_KEY`, so Latchkey's startup key-resolution fell through to the system keychain on every spawn. The per-directory encryption key is now injected into that subprocess's environment, matching the other Latchkey invocations.

Bump the pinned latchkey CLI version installed on remote VPS environments (the secondary gateway) to 2.19.1.

Added: the permission-request gateway (`POST /permission-requests`) now validates `agent_id` against the canonical `AgentId` format (`agent-` followed by 32 hex characters) and rejects a malformed value with HTTP 400 before persisting it. An agent that supplies a bad `agent_id` (e.g. a hand-crafted body with a placeholder like `ENV_AGENT`) is now notified of the failure at its request, rather than silently filing an unusable request that later crashed the desktop client's permission-requests consumer. The JS check mirrors Python's `imbue.imbue_common.ids.RandomId._validate`; a cross-language test posts a freshly generated `AgentId` to guard against the two definitions drifting apart.

## 2026-06-23

`LatchkeyForwardSupervisor` (and the underlying `spawn_detached_mngr_latchkey_forward`) now accept a `cwd` argument, so embedders can launch the detached `mngr latchkey forward` supervisor from a chosen working directory (e.g. `$HOME`) instead of inheriting the caller's cwd. This keeps the supervisor's `mngr` children from resolving project config out of a transient working directory.

## 2026-06-22

Bump the pinned latchkey CLI version installed on remote VPS environments (the secondary gateway) to 2.17.2.

## 2026-06-19

Removed the now-vestigial `LatchkeyPluginConfig.merge_with` override; the config merge is routed through the overlay pipeline, which reproduces the same assign-by-default semantics. No user-visible behavior change.

Reorganized the README: high-level user info (CLI, wiring an agent, settings, logs, permissions overview) is up top, and the deeper detail (the full gateway HTTP-extension reference and the Python embedding API) now lives in a clearly-marked Reference section below.

Bump the latchkey CLI to 2.17.1:

- The pinned version installed on remote VPS environments (the secondary gateway used by agents when the user's computer is down) is now 2.17.1.

- The minimum latchkey CLI version the wrapper will operate against is now 2.17.1, so initializing the gateway rejects any installed CLI older than 2.17.1.

## 2026-06-17

- Avoid `logger.warning()`.
- Replace `logger.debug()` with `logger.info()` to prevent logs from growing needlessly.

Fixed a bug where applying a latchkey permission grant could fail with a 500 (`ENOENT ... latchkey_permissions.json.tmp.<hex>`) when the per-host directory did not exist yet. The gateway's `permissions` extension (`POST /permissions/rules`) now creates the target file's parent directories (e.g. `hosts/<host_id>/`) before writing, matching its documented "creates the target file if it does not yet exist" behavior.

Added `maybe_recover_host_permissions_for_agent` to `agent_setup`: a best-effort repair that, given an agent's opaque permissions handle, host id, and agent id, materializes the canonical per-host permissions file (and points the opaque handle's symlink at it, recreating the handle if it had gone missing) when that file is missing, and idempotently re-registers the agent in the host's `minds-api-proxy` allowlist (closing the gap where discovery-time auto-register skipped the agent because the host file did not exist yet). Cheap in the common case (the canonical file already exists). Used by minds to self-heal hosts whose agent-creation finalize/link step was skipped or failed.

Added `point_opaque_handle_at_host` to `store`: (re)creates an opaque permissions handle as a symlink to the canonical host file without moving anything (the symlink-only tail of `link_opaque_permissions_to_host`, now shared between the two).

## 2026-06-16

Exposed the catch-all permission name as a public `WILDCARD_PERMISSION_NAME` constant (still `any`) so the minds permission dialog can present it to users as `all` while keeping the stored/granted value unchanged.

## 2026-06-15

`mngr latchkey forward` now has a structured, rotated, timestamped log, reusing the standard mngr/minds JSONL logging rather than the previous unrotated, untimestamped files.

- The supervisor now writes its structured log to `<latchkey_directory>/mngr_latchkey/events.jsonl` (one flat JSON object per line with a nanosecond timestamp, size-rotated with rotated copies pruned). Read this when you need to observe timing.

- The shared `latchkey gateway` subprocess's output is now routed through loguru (each line at DEBUG, prefixed with `[latchkey gateway]`) into that same structured log, so it is timestamped and rotated like the rest of the logs instead of accumulating in the separate, unrotated `latchkey_gateway.log`. That separate file is no longer written.

- The detached supervisor is now spawned with `--quiet`, so its raw `latchkey_forward.log` capture file no longer accumulates console output in steady state (everything goes to the structured `events.jsonl`). The raw file stays effectively empty and only ever captures rare startup-failure output (Click errors or a pre-logging traceback), which is exactly when you want it. Its fd is handed straight to the detached process, so it cannot be rotated mid-write -- keeping it near-empty is what bounds it.

## 2026-06-12

Fixed file-sharing permission grants for paths containing spaces or non-ASCII characters (e.g. an agent-requested or user-selected directory like `My Documents`). The per-file permission pattern is now built from the same WHATWG-URL-normalized (percent-encoded) form that the gateway matches incoming requests against, so a path with a space (`%20`) or accented letter now matches instead of silently never granting access.

File-sharing permission requests now accept paths that use `~` / `~/...` notation for the current user's home directory; the gateway expands them to an absolute path (the same root the WebDAV home mount is served from) before storing the grant. `~user` notation for another user's home is rejected with a clear error.

Fixed the gateway permissions extension so that services whose catalog lists no specific permissions (e.g. Linear) now surface the catch-all `any` permission. `GET /permissions/available/<service>` injects `any` as the first available permission for every scope, and `POST /permission-requests` accepts a `predefined` request naming `any` under any known scope. Previously such services appeared to have no permissions an agent could request.

## 2026-06-11

Hardened how the VPS-resident latchkey gateway receives its secrets. The encryption key and gateway listen password are no longer interpolated into the gateway start command (where they could surface in process listings or command logs). Instead they are written to short-lived 0600 files on the VPS that the start script reads into the gateway's environment and deletes immediately. This keeps the secrets out of process argv and logs, and -- importantly -- avoids leaving the encryption key on the VPS disk next to the encrypted credential store it decrypts. These temp files now also use random, non-descriptive names so they do not advertise which secret each one holds to anyone able to list the directory.

Decoupled per-agent latchkey gateway setup so a failure to reverse-tunnel the desktop-side gateway into an agent's container no longer prevents that agent's VPS-resident gateway from being provisioned (and vice versa). The two reachability paths are independent, so each is now attempted with its own error handling.

Coalesced VPS-resident gateway provisioning per outer host: when several agents share one outer host (VPS/container), only one provisioning pass runs at a time instead of multiple agents racing concurrent, redundant passes against the same host's gateway, tunnel, and credential/permission files.

Stopped re-provisioning an already-provisioned outer host on every discovery cycle. The discovery stream re-emits the full agent set continuously; previously each emission re-ran the full (expensive, idempotent) VPS gateway provisioning, flooding the log and the network with redundant SSH work. Each host is now provisioned at most once per supervisor lifetime (a failed pass still retries, and a supervisor restart re-provisions); ongoing credential/permission sync continues to be handled by the remote-state watcher.

The VPS-resident latchkey gateway now starts with the same shared password the
local desktop gateway uses. The desktop-derived gateway password (a pure
function of the shared Latchkey encryption key, as produced by
`Latchkey.derive_gateway_password`) is injected into the remote gateway as
`LATCHKEY_GATEWAY_LISTEN_PASSWORD`, matching the `LATCHKEY_GATEWAY_PASSWORD`
agents already present. Previously the remote gateway started without any
listen password, so it did not enforce the same authentication as the local
gateway.

Remote VPS gateways now receive only the latchkey credentials a host's permissions actually grant, instead of the full desktop credential store.

When syncing credentials to a remote VPS, mngr resolves the canonical services a host has been granted (mapping its permissions-rule scopes back to service names via the bundled `services.json` catalog, accessed through the new `services_catalog` module), drops any whose credentials are not actually stored (checked with `latchkey services info --offline`), and re-encrypts a host-scoped subset with the same encryption key via `latchkey auth re-encrypt --services`. The encryption key is unchanged, so the gateway's derived password and the agents' permissions-override JWTs keep validating. When nothing is left to ship (a deny-all host, or every granted service lacks stored credentials) the remote store is cleared instead. This limits the blast radius of a VPS compromise to the credentials the agent was actually permitted to use.

The `services_catalog` module also now owns the dialog-facing catalog (`ServicesCatalog` / `ServicePermissionInfo`), previously in the desktop client. It reads the bundled `services.json` directly rather than over HTTP, so the gateway's `permissions` extension no longer serves the bare `GET /permissions/available` collection endpoint (the per-service `GET /permissions/available/<service>` endpoint that agents use is unchanged).

Replaced a direct RuntimeError raise in the discovery stream consumer with a dedicated DiscoveryStreamError.

## 2026-06-09

Regenerated the latchkey `services.json` permission catalog from detent 1.5.0.
This adds the new `notion-mcp` service (Notion's hosted MCP endpoint at
`mcp.notion.com`, scope `notion-mcp-api`, displayed as "Notion (MCP)") with its
20 grantable permissions, and refreshes the Slack `slack-read-all` /
`slack-write-all` descriptions to match detent's updated wording. The catalog
generator (`scripts/generate_services_json.py`) gained curated display-name and
service-order entries for `notion-mcp`.

The VPS-resident latchkey gateway is now launched with
`LATCHKEY_DISABLE_CREDENTIALS_REFRESH=1`. The remote gateway runs on a synced
copy of the user's credentials, so disabling refresh there prevents it from
racing the desktop-side latchkey to rotate the same OAuth refresh token (which
would exhaust the user's token and invalidate the desktop's credentials). The
desktop-side latchkey remains the single owner of credential refresh.

The `permission-requests` gateway extension's approve endpoint
(`POST /permission-requests/approve/<id>`) now accepts an optional JSON body
carrying a single `path` field. When present, and only for `file-sharing`
requests, the file-sharing effect is recomputed for that path instead of using
the one precomputed at request-creation time -- this lets the Minds desktop
client honor a user who edited the shared path in the approval dialog. The
overridden path is re-validated with the same traversal-rejection rules used at
request creation, the access mode fixed at creation time is preserved (a path
override cannot escalate read-only to read-write), and only the `path` field is
accepted in the body. An empty or `null` body preserves the previous behavior
(apply the precomputed effect verbatim).

File-sharing requests are now validated to be within one of the Minds WebDAV
mount roots -- the user's home directory or the system temp directory -- at
request-creation time (and at approve time for a user-edited path override).
A grant for any path outside those roots is inert (the WebDAV server has no
provider for it and answers 404), so rejecting it up front gives the agent a
clear "must be within a shared root" error instead of an approve-then-404 dead
end. The roots are derived from the gateway process's `homedir()` / `tmpdir()`,
which match the `Path.home()` / `tempfile.gettempdir()` roots the Minds WebDAV
server serves on the desktop host. The comparison is case-insensitive (mirroring
the WebDAV share-prefix matching) and purely lexical (no symlink resolution or
existence check).

## 2026-06-08

- Now auto-discovered as a publishable package by the release tooling (it is a standalone `mngr latchkey` plugin). It will be offered for first publication to PyPI on the next release. Its stale `imbue-common==0.1.17` and `concurrency-group==0.1.17` pins are realigned to the current `0.1.18`. No runtime change.

## 2026-06-06

Added `libs/mngr_latchkey/imbue/mngr_latchkey/remote_gateway.py`, the first piece of "run the latchkey gateway on the VPS" support. It declares a pinned `LATCHKEY_VERSION` (2.15.1) and a small public surface for standing up a latchkey gateway on a remote VPS (the agent's outer host):

- `sync_credentials(host, latchkey_directory)` copies the local encrypted credential store (`<latchkey_directory>/credentials.json.enc`) to `~/.latchkey/` on the VPS so the remote latchkey CLI can decrypt the same credentials.
- `sync_permissions(host, latchkey_directory, host_id)` copies the per-host permissions file (`<latchkey_directory>/mngr_latchkey/hosts/<host_id>/latchkey_permissions.json`) to `~/.latchkey/permissions.json` on the VPS, falling back to the restrictive deny-all default when the host has no local permissions file.

Both syncs write atomically (to a sibling `.tmp` file, then `mv` into place), so the remote gateway never reads a half-written credentials or permissions file mid-sync.
- `provision_remote_gateway(host, host_id, container_ssh_user, container_ssh_port)` is the orchestrator (its internal steps are private helpers). It: installs the upstream `latchkey` CLI and its prerequisites (curl, Node.js via NodeSource, `latchkey@<version>` via `npm install -g`); starts `latchkey gateway` detached, bound to the VPS loopback on `OUTER_PORT` (`LATCHKEY_DISABLE_COUNTING=1`, and `LATCHKEY_ENCRYPTION_KEY` interpolated from the local `<latchkey_directory>/encryption_key` so the VPS gateway can decrypt the synced `credentials.json.enc`), unless one is already running; locates the agent's container on the VPS by its `com.imbue.mngr.host-id` label; mints an ad-hoc ed25519 keypair and authorizes it in the container via `docker exec` (the VPS owns the docker daemon, so no pre-existing SSH access is needed); and opens a reverse SSH tunnel from the VPS into the container (`-R 127.0.0.1:INNER_PORT:127.0.0.1:OUTER_PORT`) so the agent reaches the VPS gateway via its unchanged `LATCHKEY_GATEWAY=http://127.0.0.1:INNER_PORT`. It is a no-op when the outer host is the local machine (e.g. the outer of a local docker daemon), so latchkey is never installed or run on the user's own computer -- only genuinely-remote outers are provisioned. The install runs as a single POSIX-sh command (no bash-only `pipefail`). The detached gateway and tunnel are each launched under `nohup` and made idempotent via a PID file (`$HOME/.latchkey/{gateway,tunnel}.pid`) checked with `kill -0` plus a `/proc/<pid>/cmdline` marker -- not `pgrep -f`, which would self-match the shell running the launch script (its argv contains the launch command) and so could either never start the gateway or spuriously restart the tunnel.
- `LatchkeyDiscoveryHandler` now takes an `MngrContext`. On agent discovery, every SSH-reachable agent gets the desktop-side gateway reverse-tunneled onto its `127.0.0.1:AGENT_SIDE_LATCHKEY_PORT` (run inline; this is the only path for local agents). Agents whose host *also* has an accessible outer host (the VPS -- decided by a cheap, connection-free `outer_host_id_for` check) additionally get the heavy VPS-resident gateway provisioning thrown onto its own fire-and-forget concurrency-group thread, reverse-tunneled onto a distinct `127.0.0.1:INNER_PORT`, so a VPS agent can reach both the desktop gateway and the VPS gateway at once. The provisioning thread is unchecked so one agent's failure can't tear down the shared supervisor, but the group's `ObservableThread` logs any uncaught failure at error level so it is never silently missed, and a later discovery fire retries idempotently. The discovery callback now carries the host id.
- `INNER_PORT` is now `AGENT_SIDE_LATCHKEY_PORT + 1` (not 1989), so the VPS gateway's in-container reverse-tunnel port does not collide with the desktop gateway's in-container port.
- `LatchkeyDiscoveryHandler.start_remote_state_sync(concurrency_group)` keeps every known remote (VPS) host in sync with the desktop's latchkey state. It first syncs each currently-known remote host's permissions and then credentials (permissions first), and a newly-provisioned host gets the same initial sync inline (reusing the provisioning SSH connection). It then uses a `watchdog` observer to react to changes: a change to the local credentials file pushes credentials to every known remote host, and a change to a host's permissions file pushes that host's permissions. The observer is stopped when the supervisor shuts down; a host that no longer exists is dropped from the set. The observer's health is supervised on a *checked* concurrency-group strand: if it dies for any reason other than shutdown, that surfaces as a loud failure (the strand raises, the group surfaces it, and the supervisor is signalled to tear down) rather than silently leaving remote agents with stale credentials/permissions. Wired into `mngr latchkey forward`. Adds a `watchdog>=4.0` dependency.
- `prepare_agent_latchkey(..., is_tunneled=True)` now also injects `LATCHKEY_GATEWAY_SECONDARY` into the agent's host env: the agent's URL for the per-VPS gateway as seen from inside the workspace container (`http://127.0.0.1:<INNER_PORT>`, where the discovery handler reverse-tunnels the VPS-resident gateway). It is set for all tunneled agents (the endpoint is only live on genuinely-remote VPS-backed hosts; the URL is the agent's view either way) and omitted for on-host/DEV agents. This flows automatically to both `mngr latchkey create-agent-env` (CLI) and the minds desktop client (which lifts every `latchkey_env` entry into a `--host-env` flag).
- Adds `INNER_PORT`/`OUTER_PORT` constants: `INNER_PORT` is the in-container port on which the VPS gateway is reached (distinct from the desktop gateway's `AGENT_SIDE_LATCHKEY_PORT`) and `OUTER_PORT` is the gateway's VPS-loopback bind port (the tunnel's forward target).

## 2026-06-05

- Added to the release tooling's publish graph (`scripts/utils.py`). It will be offered for first publication to PyPI on the next release. Its stale `imbue-common==0.1.17` / `concurrency-group==0.1.17` pins are realigned to the current `0.1.18`. No runtime change.

## 2026-06-04

`mngr latchkey forward`'s discovery observer now writes to the standard discovery event log instead of a private per-env `discovery-observe` directory. It is the single discovery observer for the host dir (minds' `mngr forward --observe-via-file` tails the same log), so the previous isolation onto a separate event log -- needed only when two observers ran at once -- is no longer required. Old `discovery-observe/` directories left by prior versions are inert and can be deleted manually.

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-04

`mngr latchkey forward` now refreshes its provider set on SIGHUP instead of shutting down. SIGHUP bounces only the `mngr observe` child (the shared gateway and all reverse tunnels stay up); SIGINT/SIGTERM remain the shutdown signals. Its discovery consumer also retains agents whose provider errored on a poll rather than tearing down their reverse tunnels, dropping them only on an explicit destroy or a later successful poll. `LatchkeyForwardSupervisor` gained a `bounce()` method that SIGHUPs a live supervisor (or starts one if none is running) so embedders can refresh latchkey's provider set mid-session.

## 2026-06-03

The latchkey forward's discovery observer (`mngr observe --discovery-only`) now writes its event log to a private, per-environment directory under the latchkey plugin data dir instead of the shared mngr discovery log. This fixes workspaces flickering out of (and never reliably appearing in) the desktop UI: when latchkey's observer and another forward's observer shared one discovery log, a snapshot from one observer that didn't have the imbue_cloud provider registered (e.g. it started before the account was written to the profile) was tailed by the other observer and treated as authoritative "no hosts," repeatedly dropping live workspaces. The private log is cleared on forward (re)start so a prior run's stale snapshots aren't replayed.

## 2026-06-02

Internal refactor with no user-visible behavior change. Updated the JSON output call sites to use the renamed `write_json_line` helper from `imbue.mngr.cli.output_helpers` (formerly `emit_final_json`, now removed).

Added `libs/mngr_latchkey/scripts/generate_services_json.py`, a developer tool that regenerates the bundled `services.json` permission catalog from a detent checkout's built-in request schemas. It classifies each schema as a scope or a permission (mirroring detent's own doc generator, including the AWS special case), groups permissions under their owning scope, and carries over detent's per-schema `$comment` summaries.

Regenerated `services.json` against the current detent. Each scope entry now carries a `description` (detent's `$comment` for the scope), and `permissions` changed from a list of strings to a list of `{"name", "description"}` objects so each permission's summary is colocated with its name. The refresh also picks up detent's newer definitions: Slack gains `slack-auth-read`/`slack-auth-write`, and GitLab now exposes a separate `gitlab-git` scope (alongside `gitlab-api`), matching how GitHub is split.

The `permissions` gateway extension now documents and validates the new scope-level `description` and the `{name, description}` permission objects. The `GET /permissions/available` and `GET /permissions/available/<service_name>` endpoints surface the scope and per-permission descriptions (their documented contract was updated to match), covered by new end-to-end tests that drive the extension over HTTP.

The `permission_requests` gateway extension now validates the `scope` and `permissions` of incoming `predefined` POST `/permission-requests` bodies against the bundled `services.json` catalog. A request whose `scope` is not a known Detent scope, or whose `permissions` list contains entries that the catalog does not list under that scope, is rejected with HTTP 400 at creation time rather than persisted as a pending request that approval would happily splice into `permissions.json`. File-sharing requests are unaffected.

The two Node-driven extension tests (`minds_api_proxy_test.py`, `permission_requests_test.py`) no longer silently skip in CI. They spawn a Node child process to drive the `.mjs` gateway extensions and carried `skipif(shutil.which("node") is None)`, which skipped silently on the Node-less offload image -- so they exercised nothing. With Node now installed in the shared mngr image, the `skipif` is removed: they run on offload and assert Node is present (a missing Node fails loudly rather than skipping).

- pyproject.toml: align `imbue-mngr*==` pin stragglers with the satellites bumped in main's `e22e7010e` release commit. Several `imbue-mngr-*` libs still pinned to older versions even though `libs/mngr` had moved to 0.2.10; building the apps/minds ToDesktop bundle from main today would fail at `uv lock` in `apps/minds/scripts/build.js` because the workspace constraint graph is unsatisfiable. Day-to-day dev hides this because `[tool.uv.sources]` redirects every `imbue-mngr-*` to its workspace path, bypassing the `==` pin.

## 2026-06-01

Bump Latchkey to version 2.14.0 to support GitHub git operations via Latchkey gateway.

Changed the `services.json` catalog (and the `permissions` gateway extension that reads it) so each raw service name now maps to a *list* of scope entries instead of a single entry. This lets one service expose more than one detent scope. The `GET /permissions/available` and `GET /permissions/available/<service_name>` endpoints now return arrays of `{scope, display_name, permissions}` objects per service.

## 2026-05-28

- `Latchkey.auth_browser` now transparently recovers from latchkey's "Service `<name>` requires preparation first" error: when it sees that message it runs `latchkey auth browser-prepare <service>` and then retries `latchkey auth browser <service>` once, so callers (e.g. minds' predefined-permission grant flow) succeed on the first user-visible attempt instead of failing with a confusing error. Failures of either the prepare step or the retry are surfaced as the usual `(False, message)` result.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-26

# Minds-api-proxy: authorization injection + schema editing + baseline notification grant

The `minds-api-proxy` gateway extension now authenticates the
forwarded request *to* the upstream Minds API on the agent's behalf:

- It reads `LATCHKEY_EXTENSION_MINDS_API_KEY` on every request and,
  when set, overwrites the inbound `Authorization` header with
  `Bearer <LATCHKEY_EXTENSION_MINDS_API_KEY>` before forwarding.
  Agents therefore never see the key and cannot spoof one. With the
  env var unset, the inbound `Authorization` header is forwarded
  unchanged (used by tests).

The `permissions` extension grew matching CRUD for inline detent
schemas alongside its existing rule editor:

- `POST /permissions/schemas?path=<file>&schema_name=<name>` adds or
  replaces an inline schema. The body is a JSON object (the schema
  definition). Schema names must match the conservative pattern
  `^[A-Za-z0-9][A-Za-z0-9._-]*$` so they round-trip safely through
  URL path segments and detent's name lookup.
- `DELETE /permissions/schemas?path=<file>&schema_name=<name>` removes
  the named schema.

These let minds install per-agent path-pattern schemas (`"only
`/minds-api-proxy/api/v1/agents/<agent_id>/...`"`) at agent-creation
time without having to direct-write the per-host permissions file
itself.

The agent baseline (`_AGENT_BASELINE_PERMISSIONS` in
`mngr_latchkey/agent_setup.py`) now ships an extra permission schema
out of the box: every minds-created agent can
`POST /minds-api-proxy/api/v1/agents/<...>/notifications`. New
helpers expose the per-agent scope / permission names + inline
schemas that the desktop client adds for each agent on top of the
baseline:

- `agent_minds_api_proxy_scope_name(agent_id)`
- `agent_minds_api_proxy_permission_name(agent_id)`
- `build_agent_minds_api_proxy_schemas(agent_id)`

`mngr_imbue_cloud/host.py`'s `build_combined_inject_command` /
`normalize_inject_args` (and the helpers only they called) are
gone entirely: there is exactly one `MINDS_API_KEY` per minds
installation now, the latchkey gateway injects it transparently, and
agents never see the value -- so there was nothing left to push
down onto a leased pool host, and the functions had no caller in
the monorepo outside their own tests.

## Narrower interface for per-agent Minds API proxy permissions

The per-agent `minds-api-proxy` permissioning model has been simplified.
Instead of installing a per-agent scope schema + per-agent permission
schema + per-agent rule at agent creation time (via the low-level
`POST /permissions/schemas` extension endpoint), the baseline
permissions file carries two cooperating rules + a plain JSON list of
allowed agent ids. To allow a new agent, the desktop client (or an
operator running the CLI) just appends an entry to that list.

Concretely:

- The baseline now has **two** rules, in this exact order:
  1. `{minds-api-proxy-unauthorized: []}` -- scope matches any
     `/minds-api-proxy/api/v1/agents/<id>/...` request whose `<id>`
     is NOT in the allowed list (encoded as `not + anyOf` on the
     path schema). The empty permission list rejects the request
     immediately; detent stops at the first matching scope, so the
     rule below never gets a chance to allow it.
  2. `{latchkey-self: [...gateway-self baseline..., minds-api-proxy]}`
     -- the existing gateway-self rule, extended with a *generic*
     `minds-api-proxy` permission that matches any path under the
     proxy's `/agents/<id>/` subtree without enumerating ids.
     Authorized agents (those past Rule 1's `not + anyOf`) hit this
     rule and are let through by the generic permission.
- The source-of-truth list of allowed agent ids is a plain JSON
  `anyOf` array inside Rule 1's scope schema -- one entry per allowed
  agent of the form `{"pattern": "^/minds-api-proxy/api/v1/agents/<id>(/.*)?$"}`.
  No regex alternation parsing/building; reading the list is iteration,
  appending is `list.append`.
- New library helper: `imbue.mngr_latchkey.agent_setup.register_agent_for_host(plugin_data_dir, host_id, agent_id)`.
  Reads the host's permissions file (or starts from the baseline if it
  doesn't yet exist), extracts the existing allowed-agent list out of
  the `anyOf` block, appends a new entry if not already there, and
  writes back atomically. Idempotent.
- New CLI: `mngr latchkey register-agent --host-id ID --agent-id ID`
  wraps the helper for operators. Documented in the README's "Wiring a
  new agent using the CLI interface" section.
- `imbue.mngr_latchkey.store.load_permissions` is the new public
  reader that `register_agent_for_host` uses; symmetric with `save_permissions`.
- The shared `minds-api-proxy-notifications` baseline grant from the
  earliest design in this branch is gone entirely; notifications are
  reached via the same allowed-agent list as every other
  `/api/v1/agents/<id>/` endpoint.
- The per-agent helpers `agent_minds_api_proxy_scope_name`,
  `agent_minds_api_proxy_permission_name`, and
  `build_agent_minds_api_proxy_schemas` are gone -- nobody needs to
  mint a per-agent schema name anymore.
- The `POST /permissions/schemas` and `DELETE /permissions/schemas`
  endpoints I added to `permissions.mjs` in an earlier round of this
  branch are gone. The user-facing interface for granting Minds API
  access is now "add the agent id to the host's allowed-agent list"
  (via the helper or the CLI), not "install arbitrary inline schemas".

A permissions file whose `anyOf` block has been hand-edited into a
shape the parser doesn't recognize is left alone (the helper raises
`LatchkeyStoreError` rather than rebuild from scratch), so operators
who customize the file by hand won't lose their edits silently.

## Consolidation: shared `SSHTunnelManager`

The `SSHTunnelManager` (and `RemoteSSHInfo`, `ReverseTunnelInfo`,
`SSHTunnelError`) used to exist in two places: this package's own
`mngr_latchkey/ssh_tunnel.py` (driving the latchkey gateway's
reverse-into-each-agent tunnels) and the `mngr_forward` plugin's
`mngr_forward/ssh_tunnel.py` (driving forward + `--reverse` tunnels).
The two implementations were ~70% verbatim duplicates that diverged on
three things: latchkey added a per-tunnel exponential backoff for the
repair loop (capped at 5 minutes), an `agent_id` tag on each
`ReverseTunnelInfo`, and a `remove_reverse_tunnels_for_agent` cleanup
hook used by the destruction path.

All three latchkey improvements moved into the `mngr_forward` manager
(they're strictly better behavior for both callers), and
`mngr_latchkey/ssh_tunnel.py` is gone:

- `mngr_latchkey/discovery.py`, `cli.py`, `discovery_stream.py`,
  `discovery_stream_test.py`, and `core_test.py` now import
  `RemoteSSHInfo`, `SSHTunnelError`, `SSHTunnelManager` from
  `imbue.mngr_forward.ssh_tunnel` instead.
- The 635-line `mngr_latchkey/ssh_tunnel_test.py` has been
  consolidated into `mngr_forward/ssh_tunnel_test.py` (which now
  carries the previously-thin manager unit tests plus the new
  exponential-backoff + `remove_reverse_tunnels_for_agent` coverage).
- The reverse-tunnel repair loop in `mngr_forward` no longer uses a
  flat 30s retry; it uses per-tunnel exponential backoff with a 5min
  cap. Same recovery latency for healthy targets; much less wasted
  paramiko handshake against permanently-gone ones.
- `remove_reverse_tunnels_for_agent` is careful not to close an SSH
  client out from under any live *forward* tunnel using the same
  host, so the two flavors of tunnel can coexist on one connection.

- Internal: simplified `mngr_latchkey.store.LatchkeyPermissionsConfig` save/load to rely on Pydantic's built-in JSON serialization and validation instead of hand-rolled JSON parsing. The on-disk file format is unchanged; the model now uses `extra="ignore"` so unknown top-level keys (e.g. detent's `include`) continue to be silently dropped on load and not re-emitted on the next save.

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-25

### permission-requests extension: preserve symlinks at the approval target

`POST /permission-requests/approve/<id>` previously replaced the
target `permissions.json` with a regular file when the target path
was a symlink. This broke the per-agent opaque symlinks that
`mngr latchkey link-permissions` swings into the canonical host
permissions file: subsequent agents sharing the canonical file
silently desynced from the granted permissions.

The atomic-write helper now `lstat`s the target and, if it is a
symlink, resolves it via `realpath` before computing the temp path
and renaming. The atomic swap lands on the underlying file and the
symlink stays in place. Approving against a non-symlink target,
or a target that does not yet exist, is unchanged.

`permissions.mjs` was already safe: its write paths resolve symlinks
up front via `resolvePathParamUnderRoot`.

## 2026-05-22

- Latchkey gateway ships a new bundled `minds-api-proxy` extension that
  transparently reverse-proxies requests under
  `/minds-api-proxy` to the minds desktop client's bare-origin
  "Minds API". The upstream URL is read at request time from the
  `LATCHKEY_EXTENSION_MINDS_API_URL` environment variable, and is
  published to the detached `mngr latchkey forward` supervisor (via the
  new `LatchkeyForwardSupervisor.extra_env`) on every `minds run`
  startup, so the proxy always points at the live Minds API port even
  when minds re-binds on restart. The extension responds 503 when the
  env var is not configured; requests still go through the gateway's
  normal permission check.
- The Latchkey gateway's `permission-requests` extension grows a typed
  request schema and a new approve endpoint:
  - `POST /permission-requests` now takes
    `{agent_id, rationale, type, payload}` instead of the legacy flat
    `{scope, permissions, ...}` shape. The `type` field is
    `"predefined"` (payload `{scope, permissions}`) or `"file-sharing"`
    (payload `{path}`, absolute-only, no `..` segments).
  - Each pending request is persisted with the additional `target`
    (the extension's per-request `permissionsConfigPath`) and `effect`
    (a precomputed `{rules?, schemas?}` patch) fields. Pending requests
    live under `<latchkey-directory>/permission_requests/v2/` -- the
    `v2` segment is the on-disk schema version so future shape changes
    can land in a fresh directory rather than trying to migrate files
    in place.
  - `POST /permission-requests/approve/<request_id>` is new. It reads
    the pending request, merges its `effect.rules` (union by scope key)
    and `effect.schemas` (overwrite by name) into the stored `target`
    permissions.json (creating it if missing), and deletes the pending
    request file. Returns the fresh permissions file in the response
    body.
  - The legacy `DELETE /permission-requests/<id>` continues to remove
    a pending request without applying its effect; the minds desktop
    client uses it for the deny path.
- The `file-sharing` permission effect was rewritten to target the new
  WebDAV mount that replaced `/api/v1/file-server` on the minds side:
  - The effect no longer mints its own scope schema. The rule now
    attaches the per-file permission to the pre-existing `latchkey-self`
    scope from the agent baseline (defined in `agent_setup.py`), which
    already matches any request whose `domain` is
    `latchkey-self.invalid`. The per-file permission schema is what
    restricts the grant to a single WebDAV URL + verb set; the scope
    just identifies which rule list the permission belongs to.
  - The per-file permission schema now pins `path` (the URL path) to
    the WebDAV URL for the requested file -- the WebDAV mount serves
    each absolute path at the URL
    `/minds-api-proxy/api/v1/files<absolute_path>`, so a
    grant for `/home/user/foo.txt` matches exactly
    `/minds-api-proxy/api/v1/files/home/user/foo.txt`. The match is a
    regex `pattern` (`^<base>(/.*)?$`), not a `const`, so the grant
    also admits the same URL with a trailing slash (WebDAV clients
    commonly emit one when treating the target as a collection) and
    any sub-path nested below it: a grant on `/home/user/share`
    therefore transitively covers every file and sub-directory
    inside the share. We do not need to reject `..` segments in the
    pattern itself because the gateway feeds the permission check a
    WHATWG `Request`, and the WHATWG URL parser already collapses
    both literal `..` and percent-encoded `%2e%2e` segments out of
    `pathname` before the pattern is ever applied. The legacy
    `queryParams.path` constraint is gone (WebDAV identifies the
    file in the URL path, not via a query parameter).
  - The allowed `method` enum grew from `GET` / `POST` to the full set
    of WebDAV verbs needed to read, write, query, lock, copy, and
    delete the file: `GET`, `HEAD`, `OPTIONS`, `PUT`, `DELETE`,
    `PROPFIND`, `PROPPATCH`, `MKCOL`, `COPY`, `MOVE`, `LOCK`,
    `UNLOCK`.
  - The wire shape grew an `access` field; the agent now POSTs
    `{type: "file-sharing", payload: {path: "<absolute>", access: "READ" | "WRITE"}}`.
    `access` is required and must be one of the two literal strings
    above (case-sensitive). `READ` unlocks only the non-mutating WebDAV
    verbs (`GET`, `HEAD`, `OPTIONS`, `PROPFIND`); `WRITE` is a strict
    superset that also unlocks the single-path mutating ones (`PUT`,
    `DELETE`, `PROPPATCH`, `MKCOL`, `LOCK`, `UNLOCK`). Per-file
    permission schemas now embed the access mode and the full file
    path in their name (`minds-file-server-read-<absolute-path>` /
    `minds-file-server-write-<absolute-path>`, e.g.
    `minds-file-server-read-/home/user/notes.txt`) so a user can hold
    both grants for the same path independently and a later WRITE
    grant does not silently override an earlier READ grant (or vice
    versa). Re-approving the same `(path, access)` pair remains
    idempotent (same schema name, schemas merge by name on approve).
  - `COPY` and `MOVE` are intentionally **not** in the WRITE verb set.
    Both carry a second path in the WebDAV `Destination` HTTP header,
    and the per-file permission schema only constrains the request
    URL; granting either would let an agent write to any file under
    the WebDAV mount's share roots (`~/` or `/tmp/`) via the
    `Destination` header, regardless of what was actually shared. A
    single-file WRITE grant means "change this one file"; cross-path
    copy/move requires an explicit grant on the destination too.
    Agents that need copy semantics can `GET` the source and `PUT` to
    a destination they have a separate grant for; likewise for move
    (`GET` + `PUT` + `DELETE`).

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

- Bumped pinned `imbue-mngr` / `imbue-common` / `concurrency-group` versions to match the current monorepo.

## 2026-05-20

- The `permission-requests` latchkey gateway extension now expects POST
  bodies with the fields `agent_id`, `scope` (string), `permissions`
  (list of strings), and `rationale` in place of the previous
  `service_name` field. Pending requests are stored under
  `<latchkey-directory>/permission_requests/v1/` so any existing files
  left over from the old shape are silently ignored.
- The `permissions` latchkey gateway extension now exposes two new
  catalog endpoints: `GET /permissions/available` returns the full
  catalog as a JSON object keyed by raw service name, and
  `GET /permissions/available/<service_name>` returns a single entry
  (or 404 if the service is unknown). Each catalog value has the
  shape `{"scope": "<schema_name>", "display_name": "...",
  "permissions": [...]}`. The catalog is backed by a `services.json`
  data file that ships alongside the extensions and is materialized
  into `LATCHKEY_DIRECTORY/extensions/` together with the `.mjs` files
  at gateway-spawn time.
- The default permissions seeded for every new agent are broadened to
  let the agent read its own current permissions
  (`GET /permissions/self`) and read the per-service catalog entry
  (`GET /permissions/available/<service_name>`) in addition to the
  existing ability to file a new permission request
  (`POST /permission-requests`). The catalog read is granted under a
  path-pattern Detent permission schema (matching
  `/permissions/available/<service_name>` only) so the agent baseline
  does not also expose the unbounded collection endpoint.
- ``LatchkeyGatewayClient.get_available_services`` now returns a typed
  ``dict[str, AvailableServiceEntry]`` (pydantic-validated) instead of
  the previous untyped ``dict[str, object]``. Wire-shape validation
  (missing fields, wrong types, empty strings) now happens inside the
  client and surfaces as ``LatchkeyGatewayClientError``.

Fixed a race condition in `mngr_latchkey`'s per-directory encryption-key
resolution where a concurrent caller could read the on-disk key file
while another process was mid-write, observing an empty string. The key
file is now published atomically by writing to a sibling temp file,
`fsync`ing it, and `os.link`-ing it into the final path -- so the final
path only ever exists with complete contents.

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

Stop caching the latchkey per-directory encryption key on the long-lived `Latchkey` pydantic model. The optional `encryption_key: SecretStr | None` field is gone; instead, `Latchkey._load_encryption_key()` reads (and on first call mints) the key on every subprocess-spawn call, so the secret only lives in parent-process memory for the duration of a single env-builder + process-spawn call frame. `apps/minds/imbue/minds/cli/run.py:_build_latchkey` and `libs/mngr_latchkey/imbue/mngr_latchkey/cli.py:_build_initialized_latchkey` no longer pre-load the key at construction time.

`load_or_create_encryption_key` now validates the on-disk key file's permission bits every load. Any group or other access bit set (i.e. anything that isn't owner-only -- `0o400`, `0o600`, `0o700` are accepted) raises a new `LatchkeyEncryptionKeyPermissionError` with a copy-pasteable `chmod 600 <path>` hint, so an operator who relaxed the mode finds out loudly instead of silently leaking the key to other local users. The operator override branch (`LATCHKEY_ENCRYPTION_KEY` in the env) still wins and is unaffected. Adds `encryption_key_test.py` covering precedence, idempotence, owner-only mode acceptance, group/other rejection, and the umask-permissive minting path.

## 2026-05-14

## mngr-latchkey: switch permission management to the latchkey 2.9.0 gateway extensions

### Summary

Latchkey 2.9.0 ships two new gateway extensions that this branch wires
into `mngr_latchkey` and the minds desktop client:

- `permission_requests.mjs` -- per-process pending-permission queue.
  Agents `POST /permission-requests` when they hit a blocked service;
  the desktop client consumes `GET /permission-requests?follow=true`
  to learn about new requests and `DELETE /permission-requests/<id>`
  to clear them once granted or denied.
- `permissions.mjs` -- a `permissions.json` editor that operates on any
  file path inside `LATCHKEY_EXTENSION_PERMISSIONS_ROOT`. Used by the
  desktop client to apply per-host permission grants via
  `POST /permissions/rules?path=<host_file>&rule_key=<scope>`.

Both extensions are bundled in `imbue-mngr-latchkey` and dropped into
`<LATCHKEY_DIRECTORY>/extensions/` automatically every time `mngr
latchkey forward` spawns the shared gateway.

### `imbue-mngr-latchkey`

- `LATCHKEY_MIN_VERSION` bumped from 2.8.0 to 2.9.0.
- New extension files at
  `imbue/mngr_latchkey/extensions/{permission_requests,permissions}.mjs`,
  rewritten from the originally-supplied drafts:
    * `permissions.mjs` now takes the target file path and rule key
      via the `?path=` and `?rule_key=` query params. It requires the
      `LATCHKEY_EXTENSION_PERMISSIONS_ROOT` env var (set by
      `Latchkey._spawn_gateway` to the plugin data dir) and refuses
      any path that resolves outside it.
    * `permission_requests.mjs` no longer accepts a caller-supplied
      `request_id`; the extension generates one server-side (a
      UUID-shaped hex string) and returns it in the POST response.
- `Latchkey.create_admin_permissions_jwt()` -- materializes
  `<plugin_data_dir>/latchkey_admin_permissions.json` (idempotent,
  with the wildcard rule `{"any": ["any"]}`) and returns a cached
  JWT pointing at it. Calling code uses this JWT in the
  `X-Latchkey-Gateway-Permissions-Override` header when it needs
  full access to the gateway's extension endpoints.
- New `mngr latchkey admin-jwt` CLI subcommand wraps the above and
  prints the JWT on stdout for shell-driven workflows.
- New `mngr latchkey gateway-info` CLI subcommand that prints the
  shared gateway's URL + password as a single JSON object on stdout.
  The bound gateway port is stamped onto the existing
  `LatchkeyForwardInfo` record (`gateway_port` field) so non-spawning
  processes can discover where the gateway is listening; the
  password is intentionally **never** persisted on disk and is
  derived locally by every consumer via
  :meth:`Latchkey.derive_gateway_password` (a pure function of the
  user's latchkey encryption key).

- Bump bundled Latchkey version to 2.11.1.

Regenerated CLI docs for `mngr latchkey` to reflect current options.

## 2026-05-13

# Latchkey state is now keyed per-host instead of per-agent

Public API changes in `imbue-mngr-latchkey`:

- `imbue.mngr_latchkey.agent_setup.finalize_agent_permissions` is
  renamed to `finalize_host_permissions` and takes a `HostId` instead
  of an `AgentId`.
- `imbue.mngr_latchkey.store.permissions_path_for_agent` /
  `link_opaque_permissions_to_agent` are renamed to
  `permissions_path_for_host` / `link_opaque_permissions_to_host` and
  take a `HostId`.
- The `mngr latchkey link-permissions` subcommand takes `--host-id`
  instead of `--agent-id`.

## 2026-05-12

## mngr-latchkey: new package

Added a new `imbue-mngr-latchkey` workspace package that owns the
shared `latchkey gateway` lifecycle, per-agent latchkey wiring, and
the reverse SSH tunnel that bridges the host-side gateway into remote
agents. The minds desktop client used to host this logic in
`apps/minds/imbue/minds/desktop_client/`; it now imports the package
and keeps only its own UI-layer code (permission dialog, service
catalog, HTML templates).

The package is currently a plain Python library -- no `mngr` CLI
subcommands are registered yet.

### Python API

- `imbue.mngr_latchkey.core.Latchkey` -- single wrapper around the
  upstream `latchkey` CLI. Owns gateway spawn / adopt / stop, password
  derivation, JWT minting, services-info and auth-browser probes.
  `Latchkey.initialize()` now runs `latchkey --version` and refuses to
  continue if the installed binary is older than the new
  `LATCHKEY_MIN_VERSION = "2.9.0"` constant; misconfiguration surfaces
  immediately rather than at the first gateway spawn. Failures raise
  the new `LatchkeyVersionError` (subclass of `LatchkeyError`).
- `imbue.mngr_latchkey.agent_setup.prepare_agent_latchkey` -- assembles
  the env vars an agent needs (`LATCHKEY_GATEWAY[_PASSWORD,_PERMISSIONS_OVERRIDE,_DISABLE_COUNTING]`)
  and an opaque permissions handle. **Raises** on infrastructure
  failures (latchkey CLI broken, on-disk write failed); callers decide
  whether to abort agent creation or fall back to an empty setup.
- `imbue.mngr_latchkey.agent_setup.finalize_agent_permissions` --
  replaces the opaque handle with a symlink to the canonical
  agent-keyed `latchkey_permissions.json` once `mngr create` has
  returned the canonical agent id. **Raises** `LatchkeyStoreError` on
  failure; same policy stance as above.
- `imbue.mngr_latchkey.discovery.LatchkeyDiscoveryHandler` -- agent
  discovery callback that ensures the shared gateway is up and opens
  a reverse SSH tunnel from `127.0.0.1:AGENT_SIDE_LATCHKEY_PORT` into
  the agent. Each tunnel is tagged with its agent id.
- `imbue.mngr_latchkey.discovery.LatchkeyDestructionHandler` -- agent
  destruction callback that drops the destroyed agent's reverse tunnel
  so the SSH-tunnel health-check loop doesn't keep spinning paramiko
  transports against a host that no longer exists.
- `imbue.mngr_latchkey.ssh_tunnel.SSHTunnelManager` -- reverse-tunnel
  manager with per-tunnel exponential backoff, agent-id tagging, and
  `remove_reverse_tunnels_for_agent`.
- `imbue.mngr_latchkey.store` -- on-disk persistence: gateway record,
  permissions config read/write, opaque-handle allocation, per-agent
  symlink linking.

### Layout

Plugin metadata lives under `<latchkey_directory>/mngr_latchkey/`, keeping
it cleanly segregated from anything the upstream `latchkey` CLI writes
under the shared `LATCHKEY_DIRECTORY`. Minds uses `~/.minds/latchkey`
as that root directory.

### Dependencies

- `imbue-mngr-forward` for the bidirectional socket/channel relay
  helper (`imbue.mngr_forward.relay`), keeping the half-closed-channel
  fix in a single place rather than duplicating it.

## mngr-latchkey: register a `mngr latchkey` CLI surface

The `imbue-mngr-latchkey` package now ships as a proper `mngr` plugin:
it declares a `[project.entry-points.mngr]` entry point and registers
a `mngr latchkey` command group with three subcommands, plus a
`[plugins.latchkey]` settings.toml block.

### New CLI

- `mngr latchkey forward` -- long-running foreground supervisor.
  Spawns the shared `latchkey gateway` subprocess, consumes
  `mngr observe`'s discovery stream, and sets up / tears down a
  reverse SSH tunnel for every agent on a remote host. Stops the
  shared gateway on `SIGINT`/`SIGTERM` (coupled lifetime).
- `mngr latchkey create-agent-env` -- one-shot. Wraps
  `prepare_agent_latchkey(is_tunneled=True)` and emits
  `{"env": {...}, "opaque_permissions_path": "..."}` on stdout as a
  single JSON object.
- `mngr latchkey link-permissions --agent-id ID --opaque-path PATH` --
  one-shot. Wraps `finalize_agent_permissions` to swing the opaque
  handle's symlink to the canonical agent-keyed permissions path.

### New settings

```toml
[plugins.latchkey]
directory = "~/.mngr/latchkey"   # default
latchkey_binary = "latchkey"     # default; resolved via PATH
```

Both fields are overridable via `MNGR_LATCHKEY_DIRECTORY` and
`MNGR_LATCHKEY_BINARY` env vars and matching `--latchkey-directory` /
`--latchkey-binary` CLI flags. Precedence is CLI > env > settings.toml
> built-in default.

## mngr-latchkey: `LatchkeyForwardSupervisor`

New class in `imbue.mngr_latchkey.forward_supervisor`. Owns the
lifecycle of a single detached `mngr latchkey forward` subprocess for
a given `latchkey_directory`:

- `ensure_running()` -- idempotent. Spawns a fresh detached supervisor
  if no record exists; adopts the existing one if its PID is still
  alive and its cmdline matches `mngr latchkey forward`; otherwise
  discards the stale record and spawns a fresh one.
- `stop()` -- SIGTERMs the supervisor (which cascades into
  coupled-lifetime shutdown of the shared gateway + reverse tunnels)
  and deletes the record.
- `get_forward_info()` -- read-only inspection of the on-disk
  `LatchkeyForwardInfo` record.

## mngr-latchkey: drop the on-disk gateway record

With `LatchkeyForwardSupervisor` guaranteeing at most one `mngr
latchkey forward` process per latchkey directory, the only thing that
ever spawns a `latchkey gateway` is that single supervised forward
subprocess. Cross-process gateway adoption -- the original reason for
persisting a `LatchkeyGatewayInfo` record at `<plugin_data_dir>/latchkey_gateway.json`
-- is no longer needed.

Changes:

- `Latchkey.initialize()` no longer reads or reconciles a persisted
  gateway record. It still runs `latchkey --version` so misconfiguration
  surfaces eagerly.
- `Latchkey.ensure_gateway_started()` no longer persists / restores
  state across processes; it stays in-process-idempotent (subsequent
  calls return the cached `self._info`).
- `Latchkey.stop_gateway()` no longer deletes a record; just terminates
  the in-memory tracked subprocess.
- `imbue.mngr_latchkey.store`: removed `save_gateway_info`,
  `load_gateway_info`, `delete_gateway_info`, `gateway_info_path`, and
  the `_GATEWAY_RECORD_FILENAME` constant. `LatchkeyGatewayInfo`
  itself stays as the in-memory return-type for the spawn path.

## mngr-latchkey: spawn the gateway via ConcurrencyGroup, simplify Latchkey API

API changes:

- `Latchkey.ensure_gateway_started()` -> `Latchkey.start_gateway(cg)`.
  Takes the owning `ConcurrencyGroup` as an explicit argument (the CG's
  `__exit__` is what terminates the gateway). In-process idempotent.
- Replaced the `LatchkeyGatewayInfo` return value with simpler
  in-instance state and accessors:
  - `Latchkey.is_gateway_running` -- boolean.
  - `Latchkey.gateway_port` -- int (raises `LatchkeyNotInitializedError`
    when no gateway is running).
  - `Latchkey.gateway_url` -- `http://<listen_host>:<gateway_port>`.
- `LatchkeyGatewayInfo` itself is gone (was the in-memory return shape
  with `host`/`port`/`started_at`; replaced by the properties above).
- `Latchkey.get_gateway_info()` removed; callers use `is_gateway_running`
  / `gateway_port` directly.

## mngr-latchkey: do not let `mngr latchkey forward` die with its parent

`_forward_command` was calling `start_parent_death_watcher`, which polls
`os.getppid()` every ~3 seconds and SIGTERMs the process when the
original parent dies and the process gets reparented to PID 1. That
actively defeats the detached-supervisor pattern: when minds spawns
`mngr latchkey forward` with `start_new_session=True` and then exits,
the watcher saw the reparent and shut the gateway down within ~3
seconds.

Removed the watcher call. To still handle the *interactive* case (user
runs `mngr latchkey forward` in a terminal and closes the terminal),
SIGHUP is now wired into the same signal handler as SIGINT/SIGTERM,
so a terminal close triggers the clean coupled-lifetime shutdown path
rather than killing the python interpreter under the default handler
(which would leave the gateway orphaned).
