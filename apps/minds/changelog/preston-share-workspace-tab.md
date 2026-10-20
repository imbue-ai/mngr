The workspace titlebar's icon-tabs now open a tabbed options panel docked under the titlebar, with two tabs: Share machine and Machine settings.

The Workspace icon-tab is gone. In the desktop app the workspace is simply the view behind the panel, so a tab pointing at it did nothing; the remaining two tabs open the panel instead of navigating. The panel is anchored to the titlebar's icon-tab strip and draws its own tab strip exactly where that strip sits, so opening it reads as the strip growing into a panel rather than as a navigation. Switching tabs happens inside the panel and never reloads it.

The new Share machine tab is the single place to share a machine. It lists every app the workspace has registered plus a Whole machine entry (the workspace's own web UI), and each target keeps its own independent state: who it is shared with, whether sharing is on, and its link. Adding or removing someone while sharing is on applies immediately; while it is off, the list is staged until Enable sharing. An enabled target shows a click-to-copy link, and says so while Cloudflare is still publishing that link rather than handing over one that does not resolve yet -- including after you switch to another target and come back, which picks the wait back up instead of leaving the warning stuck on screen. A target whose sharing status can't be read says so and offers no controls, rather than presenting itself as "not shared yet"; clicking it again retries.

Links inside the panel (Machine settings' "View all backups", the sign-in prompt on an unassociated workspace) open in the app behind the panel and dismiss it, rather than loading a full page inside the panel itself.

Machine settings groups the workspace's own settings behind a General / Account / Backup nav. The per-service sharing list that used to live on the settings page is gone -- the Share machine tab replaces it. The standalone `/sharing/<agent-id>/<service>` editor still works as a direct link.

`/workspace/<agent-id>/options` is the browser-mode fallback for the whole panel and joins the in-place page-swap set. The workspace settings page keeps its own URL and now renders the same Machine settings content as the panel.

The settings gear on a workspace card in the home list now opens the options panel over the list, instead of navigating into that workspace's settings page.

The panel docks under the titlebar with its tab strip only when opened from inside a workspace, where that strip exists to hang from. Opened from the workspace list it is an ordinary centered dialog with no tabs, showing the tab it was opened on.

Destroying a workspace now deletes its Cloudflare tunnel. Nothing downstream of the host teardown knew the tunnel existed, so every destroyed workspace left one behind -- keeping a proxied hostname answering and counting against a tunnel quota that is a ceiling on workspaces ever created rather than live ones. Existing orphans still need deleting by hand.

Machine settings names the machine under its heading, so a panel opened from the workspace list says which one it is about before you press Remove machine.

Failures coming back from the imbue_cloud connector now say what went wrong instead of "see the desktop client logs for details". An expired session says so and suggests logging out and back in.

Both panel titles now read "Share machine: <name>" / "Machine settings: <name>", so a panel opened from the workspace list says which machine it is about. The name is part of the title rather than a caption under it, and truncates rather than wrapping it.

The centered dialog every overlay modal draws -- backdrop, card, close button, dismiss-on-backdrop-click -- is now one OverlayDialog component. Sign-in, Accounts, a plan, Minds Settings and sharing each had their own copy, which is how they had drifted apart.

A long workspace name in the home list now truncates instead of pushing the badges and the settings gear past the edge of its row.

Copying a share link flashes it green, the same confirmation the inspiration flow gives, since the clipboard offers none of its own. A refused copy still reports itself rather than flashing.

The waits in the share pane now name what they are doing -- checking who a target is shared with, creating the link, updating who can open it, stopping sharing -- with a spinner, rather than a bare "Loading".

An unshareable machine now says sharing runs through an Imbue account and offers a button to sign in or create one, instead of an inline text link.

The share icon is the icon set's own filled glyph rather than a scaled stroked stand-in, and the account section in Machine settings says "machine" like the rest of the panel around it.

The workspace options panel now keeps its title and its left-hand tab list in place and scrolls only the pane on the right, matching how the Minds Settings dialog already behaves. Previously the whole card body scrolled, so on a short window the title and the tabs slid out of view along with the content.

A button that starts slow work now reports it in place: the button becomes a spinner and a sentence beside or below it names what is happening. Enabling sharing says "Creating the link and granting access..." next to the button; disabling takes the link away immediately and says "Stopping sharing and revoking the link..." until the link is really gone, at which point Enable sharing comes back. Associating and disassociating an account label their own buttons "Associating..." and "Disassociating..." instead of putting the status text beside them.

Copying a share link now says so three ways at once: the link pill flashes green, its copy icon becomes a green check, and a "Copied" bubble appears above it.

The share pane's progress no longer goes stale when you switch between share targets. Each target tracks its own in-flight change, so a wait started on one no longer follows you to another that is not doing anything, and returning to a target whose change is still running finds it still reported there.

Disabling sharing also clears the "This link is not live yet" notice, which previously outlived the link it was about: the link came off screen immediately while a notice explaining that Cloudflare was still publishing it stayed behind. If the disable fails, the link and the notice both come back.

The account association prompt is no longer boxed in a bordered card. It is a message and a button, which is all it ever was; the card framed it as a separate panel inside whichever pane it already sat in.

Account association is now called linking throughout the interface: the buttons say "Link" and "Unlink", a machine with an account reads "Linked to <email>", and the waits say "Linking..." / "Unlinking...". A machine with no account now says "Link your machine to an Imbue account to enable sharing and cloud backups." on every surface that offers the prompt, replacing the two differently-worded sentences the Share pane and Machine settings each had.

Signing in from the workspace options panel no longer closes it. The sign-in modal now opens *over* the panel, which stays on screen under its backdrop, and dismissing it -- by signing in, by Escape, or by closing -- reveals the panel exactly as it was, down to the share target you had selected. Previously the panel was destroyed the instant the sign-in opened, and a completed sign-in then navigated to the workspace list behind it. The one exception is the first-run error-reporting consent screen, which still takes priority.

Signing in from the panel's Link prompt now leaves the panel showing the account you just added, instead of still asking you to add one. The panel stays mounted under the sign-in modal, so it was showing what it rendered before you signed in; a completed sign-in now re-renders it on the way back. Dismissing the sign-in without signing in still returns the panel untouched, with the share target you had selected.

The panel's left-hand list scrolls on its own when it has more entries than fit, so a machine with twenty apps no longer runs them off the bottom of the card. Each side scrolls only when its own content overflows.

Linking an account from Machine settings now leaves you on the Account group instead of dropping you back on General. Which group is open is carried in the panel's URL, the same way the tab already was, so the reload that follows a link (or a rename) comes back to the control you just used.

Focus rings are no longer shaved off by the panel's scrolling areas. The accent ring sits 4px outside the control it marks, and a scroll container clipped it for anything against an edge -- the name field, the selected colour swatch, the add-email box, the account picker. Each scrolling area now keeps enough room for it, without moving any content.

The machine name field is the design system's text input rather than a one-off pill. It previously had its own surface and corner radius and no focus ring at all, so it matched nothing else on the page and gave no keyboard-focus feedback.

The options panel's outer padding is px-6 py-4. The scrolling area's negative margin tracks it, so the scrollbar still sits on the card's edge.

Workspaces are called machines throughout the interface. The home screen lists Machines, Minds Settings groups permissions under Machines, and every message, tooltip, notice and error that referred to a workspace now refers to a machine. Minds Settings' Local files section says "on this computer" rather than "on this machine", which used to mean your own computer and would now read as one of your machines.

Only the words changed. Routes, the v1 API and its fields, element ids, CSS variables and the workspace/ directory inside a backup keep their names.

The machine name field is narrower rather than filling its row.

Fixed two identifiers the rename should not have touched: the workspace/ directory the in-place restore looks for inside a snapshot (renaming it failed every restore), and the "workspace" permission-request type shared with the mngr latchkey extension.

Unlinking an account is confirmed first, the way removing a machine is. Unlinking tears down every tunnel for the machine and cannot be undone by linking again -- sharing has to be set up from scratch -- so the button now opens a dialog that says so instead of acting immediately.

The options panel opens immediately again. Reading a machine's account re-ran `mngr imbue_cloud auth list` -- a ~1.7s subprocess -- on every page render whenever a synced record named an account that was no longer signed in, because a cache miss always forced a refresh. That refresh now happens at most once per cache generation, which is all it was ever for (recovering from a sign-in that rotated to a new user id). Measured on the machine options panel: median render 1.664s to 0.003s.

Reopening a modal that is already on screen (clicking the other titlebar tab) hands it the new URL instead of tearing the page down and loading it again, so a second click no longer discards a frame that was still loading.
