Added release channels to the desktop app, chosen from a new Updates section in Settings. **Stable** and **Alpha** are offered; Beta exists in the machinery but has no audience decided, so it is not put in front of anyone yet. One installed app, one ToDesktop app id -- switching channel never means installing a different app.

Every channel is a pointer to an already-signed, already-notarized ToDesktop build, named in `apps/minds/release-channels.toml`. Editing that file and merging is how a channel moves, so `git log -p` on it answers "what was alpha on the 12th". The manifests we publish name ToDesktop URLs, so the artifacts are never re-hosted and the digests are never recomputed.

Switching to a slower channel never downgrades you. You keep the version you are running and receive nothing until that channel catches up, which the app confirms before you commit to it. It does not warn about the resulting state: being ahead of your channel is temporary and self-correcting, and every channel already shows the version it serves. One update can still arrive after such a switch -- anything already downloaded has been handed to the installer and goes in on your next restart, which the confirmation says rather than promising a silence it cannot keep.

Nothing interrupts you to announce an update. No dialog, no OS notification: a downloaded update is offered as a small card in the corner of the window, and installs on the next restart whether or not it is ever acknowledged. The panel also reports when a check last ran, because the common outcomes -- up to date, and ahead of your channel -- both leave the screen unchanged, so a check that worked was otherwise indistinguishable from a button that did nothing.

A channel nobody has promoted a build to yet, and any channel whose feed is unreachable at the moment you look, is shown as "Unavailable right now" and cannot be selected, so a click cannot leave you on a channel that serves nothing.

The auto-updater drives `electron-updater` directly instead of `@todesktop/runtime`'s wrapper, which cannot serve channels: it deactivates itself on any build ToDesktop has not released, which is every build a fast channel serves. `electron-updater` moves from 4.6.5 to 6.8.9 at the same time. Two bugs fell out of that swap and are fixed here:

- Downgrades were permitted. `electron-updater`'s `channel` setter forces `allowDowngrade = true`, and ToDesktop's agent set it explicitly; nothing in `~/.minds` has a down-migration, so a downgrade was a data hazard rather than an inconvenience.

- "Check for Updates..." would have reported an update on every check, because the field it tested is populated on every successful check whether or not one exists.

The updater does not run when the app runs from source: an unpackaged build has no signed bundle to swap, so there is nothing for it to do, and an update offering to restart the app mid-edit is an interruption rather than a service. The update card is catalogued in the dev styleguide so its appearance can be worked on without an update existing.

Tiers opt in by setting `update_feed_base_url` in their `client.toml`, next to `lima_image_base_url`. Production is wired to `https://updates.imbueminds.com`. Reaching the installs already in the field takes one more Release in ToDesktop: nothing shipped so far carries this code, so they update through `@todesktop/runtime` until they are handed a build that reads our manifests.

"Check for Updates..." in the menu bar opens the Updates panel and checks there,
rather than answering in dialogs of its own. That panel opens even when the rest
of Settings cannot be loaded, since none of it comes from the backend -- a
machine that is not answering is when taking a different build is most likely to
be the fix.

Being parked -- running a version your channel has not reached yet -- is now
stated in the panel: "Stable is at 0.3.11, so you will stay on 0.3.12 until it
catches up." It is a plain line rather than a warning, because being ahead of a
channel is temporary and self-correcting. But it could not go unsaid: with
nothing on screen the panel redrew exactly as it does when up to date, so
switching to a slower channel looked like a click that did nothing.

The panel offers "Restart now" whenever a build is downloaded. That control
previously existed only on the floating card, which can be dismissed and does not
return for a version already dismissed -- so a finished download could be left
with no way to install it short of quitting the app by hand.

The confirmation for switching to a slower channel now says where the next
restart lands. A completed download is handed to the OS installer as it arrives,
so switching cannot take it back: the restart moves forward to the staged build
and the wait for the slower channel gets longer, not shorter. It also reads that
fact from the staged version rather than from the update status, which any later
check overwrites -- so a failed check no longer retracts a true promise at the
moment the user is weighing it. Both surfaces name a channel the way its own
radio does ("Stable"), never the bare feed name.

The Updates panel no longer reads as still checking while a download runs. The
check itself answers quickly, but the panel then asked what every channel serves
-- and the main process runs one updater task at a time, so that question was
queued behind the download the check had just started. Held under the same busy
flag, it left "Checking..." on a disabled button for the whole transfer of a
several-hundred-megabyte build. The channel query now runs after the button is
released and updates the panel when it lands. "Check now" is instead refused
while the download is in flight, which is the honest reason: a check clicked then
would queue behind the same transfer and answer minutes later.

The same build is no longer downloaded twice. An update stays "available" until
it is installed, so two checks that queue together -- the menu item and the
ten-minute interval, say -- each concluded an update was available and each
appended a download, and the second still saw it available after the first had
finished. The guard against re-downloading what is already staged now runs where
the bytes are actually fetched as well as in the check, and lives in the
electron-free channel module where it is unit-tested rather than inline in two
places.

A docked-options-panel test expected the tab order that the panel had before it
was reordered to match the titlebar (Permissions, Machine settings, Share
machine). The order it asserted no longer existed anywhere, so it failed on any
branch that ran the minds JS suites.

Both channels now point at 0.4.1 (build `26081836c9ajpkh`). This is the first
entry that is not inert: merging it publishes, moving stable from 0.3.11 and
alpha from 0.3.12.
