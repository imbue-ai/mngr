Lands the account-owned remote providers program (parts 1 through 4 of `specs/account-owned-providers/spec.md`, previously the stacked PRs #816, #836, and #845) on current main. The carried entries `mngr-fix-provider-storage.md` and `mngr-account-owned-providers-part-4.md` in this directory describe the user-visible changes (part 1 did not touch minds); this entry records what the catch-up merge changed on top of them.

- The deploy checklist (`docs/deploy/next_deploy.md`) carries this program's rollout steps: deploy the connector with migration 056 before the minds release that adds connections, give the first release that calls `/sync/documents/*` its own compat snapshot module, expect the per-account `imbue_cloud_<email-slug>` state-dir rename on first start, and switch the connector's lease stub to `imbue_cloud_<user id>` once no older client remains.

- Rebased onto the main-side desktop client as of 2026-10-11: the connections model (account instances, `<backend>_<hex>` connection instances, the providers panel sections, host access material for every backend) now coexists with the folder sync manager, the forward identity publisher, the provider sign-in relay, the local-prerequisite probes on the create form, the machine-size route, and the Imbue Studio naming. The UI schema version is bumped past main's.

- Every cloud connection block (aws, gcp, azure) carries the gen-2 container-host hardening main's bring-your-own-key blocks gained (`install_gvisor_runtime`, `docker_runtime = "runsc"`); AWS no longer writes `default_start_args`, since `mngr_vps` mounts the tmpfs itself.

- Review fixes on top of the merge: a workspace created on an aws/gcp/azure/modal connection is seeded under that connection (not the account's Imbue Cloud instance) with its per-host material; the secrets payload digest no longer depends on per-host file order, so cross-device parity stamps work for modal/vps/lima hosts; a malformed `sync documents pull` is refused instead of wiping the connection replica; the profile `settings.toml` is written at 0600 on every update; an ownerless connection registration settles the default account rather than failing on a retired one; the create form falls back to the machine's own local mode (not always Lima) when a connection loses its owner; and stale descriptions, warnings, and retired names were updated.

- The expired-session detection signs an account out only when the plugin's `auth refresh` reports the connector refused the refresh; an unreachable connector or a server error during the refresh (now reported under the plugin's connector error classes) leaves the account signed in.

- A lima host healed onto the legacy provider-wide root key (its per-host root key slots are symlinks to that key) no longer has that key collected into its workspace record's secrets, and materialization never writes synced material through such a link.

- Retrying a failed create whose connection no longer belongs to the selected account (it was re-homed to the local profile, or its account signed out) falls back to a compute option the account can use instead of pre-selecting one the server would refuse.

- Adding a connection while the plugin's account listing fails answers 503 ("Could not read the signed-in accounts ...; try again") and drops the half-added row, instead of validating against a block that was never written and blaming the pasted credentials with a 502. A sync pass that runs while a new account-owned connection's credentials are still being validated no longer pushes it to the account ahead of that validation; the add route's own push does, once the credentials passed.

- The create form's Imbue Cloud compute option is labelled through the same helper as the other modes, so it reads the way the creation transcript names it.

- A plugin accounts index that exists but cannot be read at startup (unreadable file, malformed JSON) no longer materializes as "no accounts", which deleted every account and connection provider block until the next sign-in or restart; the startup pass leaves the settings as they are and the next trigger writes them.

- The settings materializer no longer deletes every provider block when the profile's `[providers.*]` tables are split around another section (a hand-added block at the end of `settings.toml` used to make the next materialization drop the suppression blocks and the user's own blocks); the pre-import reconcile's tolerance for that layout now applies to the managed blocks too.

- A connection whose credentials payload this device cannot decrypt or parse shows as "No credentials" and stays removable; only a payload written by a newer app version reads "Needs a newer app". A connection push whose revision conflict names no stored row is never re-pushed as a new document: an edit stays queued for the next sync pass, and a removal is complete (the server already holds nothing), so its tombstone no longer retries on every pass.

- The creation page names a connection the way the compute select does (`<alias> (<Backend>)`), falling back to the instance name when the connection is gone, and the providers panel offers Add connection only under an owner the modal would keep (not under a locked account). Adding a connection answers 503 only when the account listing actually failed, not when the settings file was already converged. The workspace settings page asks for a machine size only on a leased Imbue Cloud machine.

- Retrying a failed AWS, GCP, Azure or Modal create whose connection has since been removed falls back to Imbue Cloud (or this device without an account) instead of leaving the compute select empty and the submit refused.

- A connection's Remove button stays disabled while its removal is in flight, so a second press no longer sends a second request that fails with "Unknown cloud account".

- Removing or re-pushing a connection never overwrites a copy that a newer app version rewrote on another device in the meantime: the push is refused, and the next pull shows that connection as "Needs a newer app".

- A connection whose backend this app version does not know (one a newer version added) shows in the providers panel as "Needs a newer app" instead of being left out.

- A this-device-only connection saved by a newer app version (with fields this version does not know) is kept when this version next writes the connection file, instead of being dropped together with its credentials.

- Signing out first pushes any connection removal that is still queued from a moment the connector was unreachable, so the removed connection no longer comes back on the next sign-in.

- A broken mngr settings file no longer stops background sync for the rest of the session: the sync pass that re-writes the provider settings logs the error and the next pass retries.

- A failed add-connection request whose answer carries no message (a non-JSON error page, a dropped body) is reported as "Could not set up the connection." instead of the create form's "Could not create the workspace.".

- A cloud workspace whose connection is held here but whose credentials are unusable on this device (scrubbed after a master-password clear elsewhere, or written by a newer app version) shows a "no credentials" or "needs a newer app" chip instead of staying plain with no explanation.

- Signing in from the create form no longer auto-creates on a compute the account cannot use: when the selection was a device-only connection, it is reset and the form asks for a review instead of submitting a create the server would refuse. Adding a connection under an account also clears the "Pick an account" error it satisfies.

- Merges the per-provider machine sizing branch (`mngr/per-provider-sizing`, PR #1750): the create form's CPUs / memory / disk fields and the settings page's size display and resize now coexist with the connections model. The settings page reads a machine's size from discovery for every provider that reports one, so the account-owned-provider gate that this branch had put in front of the machine-size request is gone; a Modal create passes both its per-create region and its size.

- The account lifecycle helpers that re-derive the provider blocks from the plugin's session list and apply an account's signout (interactive, and the sync pass's dead-session retirement) now live in the `connections` package (`account_lifecycle.py`) rather than in the SuperTokens routes module and the `minds run` entry point; a pure relocation.

- The sweep of orphaned per-host SSH key dirs now covers every synced key layout (Imbue Cloud, the aws/gcp/azure/vultr VPS layout, Modal, Lima, docker) and runs on positive evidence only: a dir is removed when its workspace's record is a tombstone, never because no active record names it, so a long cloud create that has not pushed its record yet can no longer lose its keys to the sweep. A dir holding a lease this device owns is still kept, and only the layouts sync lays down on the device (the account instance and the connection instances) are swept: a lima, docker or vultr host's dir, which exists on its hosting device alone, is left to the provider's own destroy.

- `POST /api/v1/workspaces` takes `modal_sandbox_timeout_seconds` for a Modal create: the sandbox lifetime for that one workspace, passed to `mngr create` as the Modal build arg `-b --timeout=<seconds>` (the per-create form of the connection block's `default_sandbox_timeout`); any other mode drops it. The `MINDS_MODAL_SANDBOX_TIMEOUT_SECONDS` process override, which rewrote every Modal connection block, is gone: the connection block always carries the day-long default and a create that wants less says so itself.

- The create form-defaults document's `machine_size_defaults_by_launch_mode` entries are the shared `MachineSizeSelection` model (the one the create request and the resize route use) instead of a private copy of its three fields; the JSON shape is unchanged.

- `WorkspaceRecordStore.imbue_cloud_host_state_dir`, left without a caller once the key-dir sweep walked the tombstoned records through the shared per-host path helper, is removed; the imbue_cloud per-host path is derived in one place (`host_access_material.host_access_paths`).

- The key-dir sweep leaves every row this device hosts alone, including a workspace on a "this device only" connection: such a row is tombstoned by local discovery alone and may still be resurrected on the next pass, and its per-host dir is the provider's own destroy's to remove, so the sweep can no longer cost a live local-connection host its only client key. It still removes the dirs of workspaces destroyed on another device.

- The create form's default Docker CPU count is lowered to what the local Docker daemon can give (read from `docker info` once the daemon is known to answer): the form submits the size it shows as an explicit `--cpus` cap, which the daemon refuses above its own CPU count, so a machine with fewer than 4 CPUs (a 2-CPU Docker Desktop) no longer has its default Docker create fail where the template's own clamped default used to work.

- `modal_sandbox_timeout_seconds` on the create request is capped at 86310 (Modal's 24h sandbox limit less the provider's shutdown buffer, the same figure the connection block's default is built from) and a longer value is refused up front (422) instead of failing in the background create.

- Picking "No account (private workspace)" in the create form's simple view while the Imbue Cloud card is selected no longer leaves that card highlighted over a compute that fell back to this device: a compute selection the account change moves clears the preset card, as a pick in the advanced compute select already did.

- A retry prefill's CPUs / memory / disk are restored only when the attempt's compute mode survived the account check: when the attempt's connection is gone and the form falls back to Imbue Cloud or this device, the next Docker or Lima pick starts from that mode's own defaults instead of the Modal attempt's size.

- A connection registration the app died inside (killed while the pasted credentials were being validated) no longer leaves a connection that this device offers as usable but never syncs to the account: on the next start the store drops the row the add never finished, and the user adds the connection again.

- The glossary's machine-size entry and the machine-size and machine-lifecycle behavior READMEs name the aws, gcp, and azure connection modes instead of the retired "bring-your-own-key" cloud modes.

- Signing an account out while the plugin's account listing fails transiently no longer tears the local half down: the account is still signed in, so its connection replica (and any queued connection change), its provider blocks and the default account are left as they are, with a warning, instead of the replica being discarded for nothing.

- The host access material module no longer restates the five per-host key layouts: each is read from the manifest its provider publishes (`VPS_HOST_ACCESS_MANIFEST`, `MODAL_...`, `DOCKER_...`, `LIMA_...`, `IMBUE_CLOUD_...`), so a provider that renames a key file or moves a store changes what the workspace record carries in one place. minds now declares `imbue-mngr-lima` as a dependency, as it already did the docker, modal and vps plugins.

- A connection's credentials can be updated in place from `PUT /api/v1/desktop/cloud-accounts/<instance>`: the connection keeps its id, instance name, owner, backend and placement, so rotating a cloud key no longer means remove and re-add (which orphaned every machine tagged with the old instance). The new credentials are validated like a new connection's and, for an account owner, pushed; a failure puts the previous credentials back, and an update whose connection was meanwhile removed on another device answers 409 instead of resurrecting it. An app killed mid-update restores the previous credentials on its next start, and no sync pass pushes or overwrites a row whose new credentials are still being validated; removing the connection, or starting a second update, while that validation runs is refused (409, `connection_in_use`) instead of leaving a tombstone that the next start would turn back into the previous connection. An account that signs out while one of its connections is being updated no longer leaves that update's request failing with a server error: the route answers 409 saying the connection is no longer on this device.

- Removing a connection, signing an account out, and the sync pass's retirement of an account whose session expired all wait for a workspace still being created on the connection (or on any of the account's providers): the create's later `mngr` calls need the provider block, and removing it mid-create left a VM in the user's cloud with no record and no block. The providers panel counts such creates into the connection's "in use by N".

- A sync pass no longer writes back an account that signed out (or was retired) while the pass was busy pulling: the provider blocks are derived from the account list the materializer reads itself, under its own lock, on every pass.

- A connection removal queued while the connector was unreachable now survives signing the account out: the tombstone is kept and lands on the first reachable pass after re-signin, instead of the connection coming back on the next pull.

- A "this device only" connection is a device provider like docker and lima: the create form may use it under any account (the selected account stays the workspace's association), and removing it is refused while a workspace recorded under any account still uses it. The create form's account options now say whether an account is locked here and whether it can own a synced connection.

- The add, update and delete connection routes answer with a `code` (`master_password_required`, `connection_in_use`, `connection_removed_elsewhere`, `credentials_rejected`, `sync_failed`, `account_listing_failed`) beside the message, so the add-connection modal can say what actually went wrong instead of blaming the keys for every failure.

- Retrying a failed create whose connection has since been removed says so ("The connection this attempt used was removed, so it cannot be retried.") instead of silently falling back to Imbue Cloud or the local mode.

- The create form defaults carry the connection backends with their labels and whether their region is pinned, and each providers-panel connection row carries its backend label, so the frontend no longer restates that table.

- A remote machine tile's state now travels with the detail behind an `error` state, so the chip can show why the machine's key could not be materialized. The UI schema version is 27.

- The first start after this release clears the `is_enabled = false` the retired providers-panel toggle could have written into the docker, lima and vultr blocks of the minds profile, once; a later deliberate hand edit is left alone.

- A this-device-only Modal connection may name its Modal namespace (`modal_user_id`, up to 48 characters) when it is added, so the evals harness's trial environments carry the run stamp the scheduled sweep matches; under an account owner the field is refused, since a synced connection must derive the same environment on every device.

- A new integration test runs the real `mngr observe` against an account block whose connector refuses the session refresh and pins that the plugin's auth error reaches the sync pass with the type name it matches.

- The create form's "add a ... connection" rows, the add-connection modal's provider list and its region rule, and the providers panel's backend labels all come from the server's backend table (`connection_backends` on the form defaults, `backend_label` on each connection row) instead of a copy kept in the frontend; the creation page names a connection's backend by the label the attempt detail now carries (`cloud_account_backend_label`, in place of the raw backend name).

- The Modal notes on the create form and in the add-connection modal now read "Modal sandboxes stop after about a day."

- A remote machine's access chip on the home screen carries a tooltip: the failure detail behind a "sync error", "Enter your master password to reach this machine" for a locked one, "The connection this machine runs on was removed; re-add it or remove this machine from the list" for one whose connection is gone, and the chip's label spelled out for the rest.

- The create form's compute select lists this device's connections under every account, after the account's own, suffixed "(this device only)"; adding a connection from the form never changes the account picked for the workspace (a connection added under another owner is offered once that owner is picked), and a locked account says so under the compute select ("Locked: enter your master password to use this account's connections"). The add-connection modal keeps an eligible preselected owner and otherwise selects "This device only" with a note naming the account that cannot own a synced connection here.

- The add-connection modal says what actually went wrong, by the error code the connection routes now send: a missing master password points at Settings (or this device only), rejected credentials keep the provider's message above "Nothing was saved; fix the keys or permissions and try again.", a failed account sync blames the network rather than the keys, a failed account listing asks for a retry in a moment, and anything else reads "Could not set up the connection." with the server's message when one came back.

- Retrying a create whose connection has since been removed: the create form shows the reason as an error banner and applies none of the recorded settings (it opens as a fresh form), and the creation page's record view disables Retry with the same reason while Discard (or Dismiss) stays available; the attempt detail carries the reason (`retry_unavailable_reason`) beside the form prefill's `unavailable_reason`.

- Each connection row in the providers panel gains an Edit button (enabled while the connection's credentials are usable here and no removal is in flight) that opens the connection modal in an edit mode: owner, provider and region fixed and shown, the alias prefilled, every credential field empty and required, saved with `PUT /api/v1/desktop/cloud-accounts/<instance>`; the panel refreshes from the next providers frame.

- Signing out is also refused (409, the same message) while a workspace is still being created on a device provider (docker, lima, a this-device-only connection) under the account: the create associates its workspace with the account when it finishes, which a signout in between silently lost, leaving a private workspace the user did not ask for.

- A connection row's placement in the providers panel omits keys without a value (a Modal connection no longer carries a `user_id` of "None").

- When a managed provider instance leaves this device (a connection removed or re-homed on another device, an account signed out), discovery forgets its hosts at once, including the persisted last-good topology, and ignores any later discovery event for the instance (the events-file replay keeps a vanished instance's last snapshot in its window forever): those machines otherwise stayed on the home screen as running, local machines of this device, and came back on every start.

- A workspace record another device hosts is never rewritten by this device, even while a stale discovery view still lists its machine here; before, two devices could stamp themselves as the host in turn and push a new revision of the same record on every sync pass.

- Selecting text inside a dialog no longer dismisses it: a press that starts inside the card and ends on the backdrop (a text selection dragged past the card's edge) used to count as a backdrop click, which closed the connection modal while the user was copying its error message. A dialog now closes only when the press, the release, and the click itself all land on the backdrop, so a press on the dim that is released over the card keeps it open too, and a right-click on the dim does not arm a later keyboard click inside the card to close it.

- A connection whose credentials the provider rejects, or whose document could not be pushed to the account, now leaves the provider's message in the backend log as a warning beside the 502 the modal shows.

- The create form's "Your own cloud" preset (from the onboarding's three where-to-run answers) opens the advanced view on the first connection the compute select offers under the selected owner (the account's own connections, then this device's), or on Vultr when there is none; the Create from Template page's compute select lists the same rows as the create form's (Imbue Cloud is offered but unusable without an account) and, like the create form's, moves off a connection the newly picked account cannot use.

- The add-connection and edit-connection dialog opens on a "This is an alpha feature" callout: launching remote machines on AWS, GCP, Azure and Modal is still being tested, and the callout links to the Imbue Discord so users can tell us how it went.

- The create form and the Create from Template page warn, beside the Create button, that a workspace on any compute other than Imbue Cloud (this device's Docker or Lima, Vultr, or a cloud connection) can take at least ten minutes to start.

- A Modal create no longer pins a region by default: the region picker leads with "Any (cheapest)", which passes no region so Modal places the sandbox wherever it has room, and "Nearest to me", which resolves the nearest of Modal's broad regions at create time, ahead of the named regions. Any is the default; the choice (not the region it resolved to) is remembered as the last-used pick, and `POST /api/v1/workspaces` accepts `any` and `nearest` as the Modal `region` beside the region names.

- The create form's "Enable sharing and web access" switch and the Imbue Cloud backup provider are on by default whenever an account is selected, and the "This computer" and "Your own cloud" presets no longer switch backups to "configure later". Picking "No account" turns both off (the server refuses either without an account); picking an account again turns them back on unless the user had set them by hand. A retry of a failed attempt keeps the attempt's backup provider the same way (its restic repository is not replaced by the account's Imbue backups default). The Create from Template page and the first-run cloud create follow the same defaults.

- A share that could not be brought up right after a create (the connector refused it, the quota is full, the network dropped) is reported in the notification feed, naming the workspace and pointing at its Share panel, instead of only a log line.

- Discovery now drops events, and the startup replay, for every provider this device does not poll, not only for a removed account or connection instance: the five suppressed defaults (`aws`, `gcp`, `azure`, `modal`, `imbue_cloud`, pinned `is_enabled = false`) and any derived instance without a block. The persisted last-good topology is pruned of such providers' hosts at startup too, so a machine of a provider that stopped being polled before this release (or whose block an older build removed) no longer comes back on every start as a row that reads "Status unknown". Backend defaults such as `local`, `docker` and `lima` are polled without a block and keep passing through.

- A live machine whose host refused this device's access key (discovery reports it unauthenticated) carries an "Access refused from this device" chip beside its unknown status, saying that a restart will not help and to open it from the device that created it or re-sync its record.

- Adding or updating a cloud connection now streams the cloud setup's output into the dialog: `POST /api/v1/desktop/cloud-accounts` and `PUT /api/v1/desktop/cloud-accounts/<instance>` answer 202 with an operation handle (the connection's instance name) once the row and its block exist, a worker runs the `mngr <backend> prepare` (or Modal listing) and the push, `GET /api/v1/desktop/cloud-accounts/operations/<instance>` reports the outcome (with the `code` and the status the synchronous route used to answer), and `.../logs` streams the command's output as server-sent events through the same replayable log buffer the create flow uses. The add-connection dialog's progress view gains "Show details" with the live output, so a 10-minute Azure first-time setup no longer looks like a frozen spinner, and the request no longer holds a server thread for that long. Callers of the bearer API poll the handle like a create. While a registration or an update is still validating, a removal or another update of that connection is refused (409, `connection_in_use`) before it touches the row.

