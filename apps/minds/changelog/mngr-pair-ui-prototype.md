**Local files** is now a list of the paths you have shared, one card each. Under the path are the two ways an agent can reach it, as a choice:

- **Allow agents to do these on-demand** — the file server, reachable while your computer is awake and Minds is running. Its dropdown is the access: *Read*, or *Read and write*.

- **Keep a copy on the machine** — the machine gets its own copy, so agents can still reach it while your computer is asleep or offline. Changes are carried across while Minds is running.

Picking the second does not switch the first off. The choice marks which one agents will normally use.

Only folders can be kept in sync. A shared file says so instead of offering the option.

A synced folder lands under `~/synced_folders/<device id>/` on the machine, mirroring its whole path on your computer — so `/Users/me/notes` becomes `~/synced_folders/host-abc/Users/me/notes`. The whole path keeps two folders with the same name apart, the device id keeps two computers' folders apart, and the home directory keeps synced files out of the machine's git checkout.

The status now distinguishes **Syncing** (turning arrows, while changes are actually being carried across) from **Synced** (a check, once everything is up to date) — which is where a sync spends nearly all its time. Turning sync on shows **Starting** straight away rather than holding the pane while the machine is contacted.

The two questions a sync raises — which way changes are copied, and which side wins when both changed the same thing — appear only once you turn it on, and changing either restarts the sync on the new setting.

The same choice is offered when you approve an agent's request for a path.

**Add file** and **Add folder** at the foot of the list open the native picker and share what you choose, read-only to begin with. Or tell an agent to request access, as before.

Syncing never touches git, and syncs stop when Minds quits.

The round trip that makes room for a sync on the machine is now timed in the logs, so a slow start can be attributed rather than guessed at.

Syncing now pairs with the machine a workspace runs on rather than with the workspace itself, and never starts a stopped machine to do it. Turning sync on for a workspace whose machine is stopped reports that instead of quietly starting it.

Folders you keep synced now come back when you reopen Minds. The choice is remembered per workspace; turning sync off, or stopping sharing the folder, forgets it. Restoring never starts a stopped machine — a folder whose machine is off says so on its row and is tried again next time.

Turning off syncing for a folder no longer throws away the machine's copy of it. The copy is set aside, and turning syncing back on picks up where it left off rather than re-fetching everything. While syncing is off the row says the copy is still there and offers to delete it, which is a separate, deliberate step because it cannot be undone.

Turning syncing on and off, and deleting a copy, all return straight away: the switch shows what you asked for and the status beside it says how far along it is (Starting, Deactivating, Deleting).

Fixed: a folder added from the Local files pane disappeared from it moments later. The grant was written on this computer but never handed to the workspace's own machine, and the next time the pane was opened it adopted the machine's policy — which had never heard of the folder — back over it.

The copy a stopped sync left behind now sits in an indented panel under the option it belongs to, rather than looking like a second section of the row, and its Delete button now uses a soft red fill instead of a solid one — Remove, which takes the whole path away, is the row's one solid red button.

A folder that is syncing now says how far through it is — `Syncing · 12.0 MB of 17.0 MB` — which matters for a big folder that would otherwise just say it was busy. The numbers are unison's own, so showing them costs no extra reading of the disk on either side.

Fixed: turning syncing off for a folder, deleting the machine's copy, then turning syncing back on left the sync unable to start at all — unison still held a record of the copy that had been deleted, and stopped rather than risk deleting the local folder to match. It now starts cleanly and re-copies the files.

Fixed: while a folder was syncing, the Permissions pane made a connection to the workspace's machine every two seconds, which made clicks in the pane stutter and put steady load on the machine. The sync status was never on the machine to begin with — it comes from the syncing process running on this computer — so the pane now asks for it without leaving this computer.

The Local files row is simpler. Access (**Read** / **Read and write**) is now a plain setting, and keeping a synchronized copy is a checkbox beneath it — an extra thing a shared folder can have, rather than an alternative to sharing it.

Which way changes travel is no longer a question, because it said the same thing as the access twice. Read means changes go from your computer to the workspace; read and write means both directions. The row states which, in those words. Changing the access moves a running sync with it.

The workspace-to-this-computer-only direction is gone. A synced folder is always seeded from a folder on your computer, so a sync that only ever copied the other way would begin by emptying it.

Clicking the checkbox repeatedly no longer queues up one operation per click, or drops the ones that arrive while Minds is busy. Each click records where you want the folder to end up; when the move in flight finishes, Minds goes wherever the last click asked for.

Changing the clash rule on a running sync now replaces just the syncing process, instead of stopping the sync (which renamed the copy on the machine out of the way) and starting it again.

Removing a shared folder from the Local files list now deletes the machine's copy of it as well. Nothing is left to offer that from once the row is gone, so a copy left behind would simply sit on the machine's disk, invisible. The button says which it will do: **Revoke access** when the machine holds no copy, **Revoke access and remove copy** when it does.

The Local files row is drawn as bands now — the path, then one per setting, divided by rules that reach both edges — instead of a stack of controls with an indented grey panel hanging off one of them. Everything the sync checkbox has to say sits behind a short rule under it, at one left edge rather than two that were three pixels apart.

Wording: the access setting reads "Agents on this machine may *read only* / *read and write*". What syncing does is now said in those same words: "Since agents on this machine may both **read and write** the folder, Minds synchronizes changes between your computer and this machine in both directions." Deleting the machine's copy is "Remove copy" throughout, in the same words the row's own button uses.

Starting a sync and restarting one now read differently. A restart is what a changed setting needs, and the files are already on the machine, so the row says **Restarting** rather than **Starting** — the wait is a handshake, not a folder being copied across.

**Deactivating** is now **Stopping**, and a sync that is simply off shows no status at all: the unticked checkbox already says so, and a "Stopped" badge beside it only asked you to reconcile the two.

The clash dropdown names what it keeps rather than where it is: **The change from your computer** / **The change from this machine**, in place of "This computer" / "The workspace".

The remove button keeps its wording while it works — **Revoking...** or **Revoking and removing...**, matching whichever it set out to do.

While a folder is syncing, the pane now refreshes once a second instead of once every two. That is how often `mngr pair` reports a transfer's progress, so the byte counts on a big folder now move as fast as they are measured. It costs nothing extra on the machine: the endpoint behind it answers from the desktop app alone, and nothing refreshes at all while no sync is live.

The folder-sync API moved out from under the permissions routes. It now lives at `/api/workspaces/<id>/folder-syncs` — `toggle`, `discard-copy`, and a `GET` on the collection for the pane's poll — because a sync is not a permission and none of the three touch latchkey. The naming is consistent with it: the routes said "path" where the feature only ever accepts folders, and their request models followed.

Two folders that overlap can no longer both be synced with the same workspace. Their copies would nest on the machine, and then each sync's ordinary housekeeping would look like a deletion to the other — turning the inner one off renames its copy aside, the outer sync reads that as the folder being deleted, and propagates the deletion back to your computer. Whichever folder you turn on first keeps the sync; the other says so instead of starting.

Overlapping folders synced with *different* workspaces are still allowed — the copies land on different machines and cannot collide — but the row now says what it costs: the same files cross the network once per workspace, and because each sync settles clashes on its own, the result may not be the one that folder's clash setting names. Syncing the exact same folder with two workspaces is still refused, for now.

Symlinks inside a synced folder now stay on the side they are on rather than being copied across. The two sides are different computers, so an absolute link names a path that need not exist on the other one, and a relative link can point outside the folder, where the two sides do not agree what it reaches.

Fixed: clicking the sync checkbox many times in a row could make Minds stop moving the folder altogether. The worker had a budget of moves per folder, and a user who kept flipping could spend it — after which the folder stayed where it was, the checkbox showed what had been asked for, and nothing would ever reconcile the two. The budget now counts moves towards *one* destination, so each click starts it again; it still stops a folder that is genuinely going nowhere, and says so in the log.

Two workspaces can now each keep their own synchronized copy of the same folder. They land under different machines' home directories and cannot collide, so the only thing that stopped it was bookkeeping — Minds held one sync per folder rather than one per folder-and-workspace. The row still warns that the same files then cross the network once per workspace, and that each sync settles clashes on its own.

The three folder-sync API routes moved into a module of their own, next to the permissions routes they used to share a file with. No behaviour changed.

Fixed: on macOS, where the filesystem usually treats `~/Work` and `~/work` as one folder, the check that stops two overlapping folders syncing with the same workspace compared them as different. Two copies would then nest on the machine, and turning the inner one off would delete the folder on your computer.

A synced folder that is a git repository arrives on the machine without its history, and the row now says so. That was already true — `mngr pair` excluded `.git` whatever you asked for — but it was a side effect of a flag about something else rather than a decision, and nothing told you. Minds now asks for the exclusion by name: `.git` is a database whose invariants span files, and a live two-way file sync has no notion of a transaction, so copying it would carry `index.lock` across and block git on the other side, and land refs pointing at objects that had not arrived yet.

Fixed three things about a sync that could not be brought back when Minds restarted. The row showed a ticked checkbox with no status at all, unticking it failed with "is not syncing", and there was no way to try again.

- A sync Minds could not restore now says so on its row, with the reason, instead of only in the log.

- Turning syncing off always works. It is a wish about where the folder ends up, and the user is entitled to it whatever state the app has got into — including one where there was never a sync to stop.

- A status is always shown for a folder whose checkbox is ticked. Only a sync that is *settled* off shows nothing, because the unticked checkbox already says it; a ticked box over a stopped sync is the app saying one thing and doing another, which is when you most need telling.

- A sync that failed now offers **Try again**. Flipping the checkbox off and on would have worked, but it sets the machine's copy aside and fetches it back for nothing.

A folder that should be syncing and is not now says **Not running** rather than **Stopped**. The two had been spelled the same, which is why a sync Minds could not bring back showed no status at all: *stopped* means the sync ended because it was asked to, and a row whose checkbox is unticked needs no badge to say so. A folder you still want synced with nothing running is a different thing, and says so.

A folder that cannot be synced now says so before you click. The checkbox is greyed out and the explanation beneath it is replaced by the reason — the folder is a file, it is inside another folder this workspace already syncs, Minds cannot yet tell which machine the workspace runs on. Ticking it used to be the only way to find out, and the refusal arrived as a message at the top of the pane. The reason shown is the one starting the sync would have given, asked of the same code, so the pane can never grey out a folder that would have worked or offer one that would not.

The warning about a folder another workspace also syncs is now yellow rather than red, says "the result may be unpredictable" rather than naming the clash setting, and stays current: it used to be fetched only when the pane opened, so the workspace that started syncing *first* would not hear about the second one until its pane was reopened.

Fixed: no synced folder came back when Minds restarted, and the row said "Minds does not know which machine this workspace runs on yet". Which machine a workspace runs on comes from the discovery pass, which has not run when the app starts, so restoring asked before there was an answer and gave up. It now waits for the answer — on its own thread, holding nothing up — and only gives up if it never arrives.

A folder whose sync is turned *off* now also blocks syncing a folder inside it, where before only a running sync did. Turning a sync off renames its copy rather than deleting it, into a tree that mirrors the live one — so two overlapping folders nest there too, and turning the outer one back on would move its whole directory across with the inner one's copy inside it. Removing the copy is what gets it out of the way, and the row says so.

Fixed: a folder inside one this workspace already syncs was still offered the option, if it had ever been synced itself. Having been synced before says nothing about whether it may be synced now, and a folder whose copy was removed while its parent started syncing is exactly the row that must not be offered.

The note about `.git` not being copied is gone from the row — it was making an already long explanation longer. The warning about another workspace syncing the same folder now appears only while this folder is actually syncing, since it describes what two running syncs do to each other.

The permission approval dialog no longer offers to keep the granted folder in sync. It had its own checkbox and its own pair of dropdowns, and they drifted: it still asked which way changes should travel, months after the Permissions pane stopped asking — so it would offer "Both ways" on a read-only grant, which the pane would never do. One place to configure a sync is better than two that disagree, and the Permissions pane is that place.

Starting a sync no longer re-runs the checks a shared path goes through. It checks what the next step actually needs — an absolute path, to a folder that exists — and nothing more. The other checks read as a security boundary and were not one: the caller they would most need to police is the restore at launch, which reads a file sitting beside Minds' own signing key and latchkey credentials, so anything able to edit it can read those and reach the workspace directly. A check that stops nobody but implies a perimeter is worse than no check.

Adding a shared folder, and changing what agents may do with one, now show the same spinner every other write in the pane shows. Both are a round trip to the workspace's own machine — the grant is not made until that machine has taken it — so a dropdown that showed the new value the instant you picked it was showing something that was not true yet. They lock the other controls while in flight for the same reason the rest do: a write answers with the whole pane, and two at once would fight over what is on screen.

The access dropdown now looks unavailable while its change is on its way to the machine, rather than only behaving that way. It was already disabled, but a plain disabled `<select>` goes on looking exactly as it did, so waiting read as a click that had not landed.
