The workspace-creation loading page now walks you through minds while the workspace is created.

Top strip (always visible): a "Setting up your machine" title above the progress bar, the live stage caption, and the collapsible logs. The bar runs no wider than the illustration below it. The workspace is entered the moment it is ready, so there is no Begin button and the bar needs no separate finished colour.

The stage caption and the details toggle appear once, in that strip. They had been showing twice -- again at the foot of the page, left and right -- because the walkthrough's strip and the page's original footer both carried them. Only the strip's pair was ever wired up, so the caption at the foot never advanced past the stage the page was first served with, and its toggle did nothing at all.

Opening the details no longer pushes the page into a scroll. The logs take a fifth of the window, which the walkthrough at full size has no room for, so while they are open the walkthrough compacts: the illustration scales down and the spacing around it closes up, making room where the logs need it. The panel's height is a share of the window rather than a fixed 200px, since how much room there is to make depends on how tall the window is.

The walkthrough plays itself: nine steps, held for 7 seconds each (10 for the chat, 9 for publishing, and 16 for the permissions step, which plays a longer sequence), with nothing to press to begin. Below the illustration sits a row of dots, one per step, each in a fixed-width slot so nothing shifts: the current step's dot stretches into a pill whose fill runs out as the step does. Any dot can be clicked to jump to its step.

Each step leads with a large bold headline (the 24px heading role) over a line of supporting copy at 1rem -- the copy sits off the type scale deliberately, so the walkthrough's message is not the smallest text on the page.

Steps:

- "Minds is your personal AI operating system. / Learn your way around while your machine sets up.", over the minds mark.

- "Your machine is where you create with agents.", with a second line and a picture per launch mode: a local machine says "This one runs right on your own computer." over this laptop with a star on its screen, and a cloud one says "This one runs in Imbue's secure cloud, but is dedicated to you." over the laptop (labelled Your device) linked by a two-headed arrow to a server rack (labelled Imbue's secure cloud), drawn in the same stroke system as everything else.

- "Agents can help you make personal tools. / Describe what you need, and your agents will make you apps to get it done.", over a chat: the request types itself out, backspaces and tries another thing -- a custom "to do" app, filtering email, then a dashboard, which it stays on, since that is the app the next step opens. The agent answers "You've got it!". Advancing carries the exchange over: the same bubbles are what the window's chat pane contains, so the frame simply draws itself around them.

- "Agents and tools run in tabs. / Get your TODOs done, clear out your email, make a dashboard.", over a workspace window where the agent pulls the app up beside the chat: no pointer and nothing clicked. "Pulling it up in a new tab." lands in the chat -- it is new on this screen, so it announces itself -- the chat column narrows, and the dashboard opens in the right half behind a clear vertical divider, its tab sitting over that half so each half carries its own tab. The dashboard is two charts that pop up in sequence, a bar chart growing out of its baseline and a line chart that draws itself on, with the area under it fading in and the endpoint dot popping last.

- "Agents can get data from your other apps. / Connect to Slack, Notion, Gmail, or browse the web to complete tasks.", over a cloud spinning app icons from the bundled latchkey services catalog, inlined into the page so they appear with it.

- "Your credentials remain safe. / Agents can only perform actions you approve.", over a scene that plays out: a dashed boundary sits between the machine and the cloud from the start, and a permission request waits on the left with Deny and Approve on it. A pointer crosses and approves; the green button holds a moment, then travels over and settles as a link through the boundary, which carries light pulses up and down as live traffic. The pointer then crosses back to Deny, which travels the same way and lands on the link as an X, closing it.

- "Share access with your teammates, friends, or even your phone.", over a laptop with an arrow drawing across to a phone, where the laptop's interface then appears. The second line depends on where the machine runs: a cloud machine can be reached with the laptop closed, a local one only while this computer is on.

- "You can publish your apps, or adapt what others have made. / Find others' apps in the Inspirations catalog here.", over two identical machines either side of a cloud. The app inside each is tinted differently, so what differs reads as the app rather than the computer; an arrow draws up to the cloud and only then does the published copy appear there, then an arrow draws down and only then does their version appear. Both arrows stay once drawn.

- A closing step for the rotating tips, on a screen of their own with no illustration: a large "Hang tight — your machine is nearly ready." takes the graphic's place.

Anything in the walkthrough can be hovered for a short explanation: the minds mark, the machine drawings, each demo tab and pane, the app cloud, and the credential-protection line.

Workspace color selection is unchanged: the create form's auto-chosen hidden color is used as before (no picker anywhere in this flow).

The page no longer scrolls: it was sized to a full viewport height on top of the titlebar's 38px offset, so it always overflowed by exactly the titlebar's height and could be scrolled with nothing to scroll to. It is now sized to the region below the titlebar and sits still; windows too short to fit the nav can still scroll the content area so the buttons stay reachable.

The tips change every 7 seconds, and the rotation starts when the last step is reached rather than at page load, so the first tip gets its full turn instead of being swapped out moments after it appears.

The rotating tips on the last step say what you can do rather than where to click -- running several agents at once, running them in the background or on a schedule, sharing a machine or a single app, viewing and revoking permission, keeping several machines, backups, stopping and restarting a machine, and reporting a bug. Naming menus and labels would go stale as soon as one moved.

The pictures are drawn to one system. Each lives in its own viewBox at its own size, so a stroke declared once came out at a different thickness in each -- the cloud read at 1.5px beside a laptop at 4.2px, and the same laptop was heavier on one step than another. Every outline is now 2.5px and every inner detail 1.5px, held there by non-scaling-stroke so the weight does not follow the scale. Arrowheads are filled rather than stroked, so nothing overlaps a line end or doubles up where a semi-transparent cap crosses one. The clouds were also drawn to a viewBox tight against their own path, which clipped away half the outline at the top, bottom and sides (the "extra thin" edges), and were stretched out of their proportions; they now have room for their stroke and keep their shape.

The drawings are inked in a flat gray rather than the theme's tertiary text and border tokens, which are translucent: a line crossing another compounded their alpha, so a join read darker than either line, which is what made the arrowheads look stuck on top of their lines.

Follow-up fixes after the walkthrough merged:

- The dive-into-the-picture zoom on entering the workspace keyed off the connections step, which stopped being the last picture when the walkthrough grew to eight steps, so it effectively never played. It now plays whenever the step on screen has a picture, and only the tips step (which has none) enters directly.

- The sse-redirect release test still clicked the walkthrough's old Next/Begin buttons, which no longer exist; it now waits for the automatic entry. The launch-to-msg e2e script waited for the old "Setting up your workspace" heading; it now matches "Setting up your machine".

- Dropped a data attribute and a CSS class that nothing read, and corrected comments describing the walkthrough's earlier seven-step form.

Ported into the Mithril SPA, merging in the meantime migration off the legacy JinjaX pages:

- Every step of the walkthrough above -- the nine scenes, the copy, the split-view dashboard, the permissions sequence, the app-cloud wheel, the rotating tips, the details-open compaction -- is now built as Mithril components under `frontend/src/views/pages/creating/`, driving the walkthrough CSS that had already been carried over into the SPA's `style.css` (which had drifted from the legacy version's later split-view rework; brought back in sync as part of this port). `frontend/src/models/walkthrough.ts` holds the four timer-driven pieces (the step auto-advance, the chat retyping, the tip rotation, the app-cloud wheel) as small, independently start()/stop()-able classes with unit tests of their own -- the SPA's page previously had none.

- Each scene that replays its sequence on every visit (the chat retyping, the connections/sharing/publishing animations, the app wheel) now does so by mounting a fresh, freshly-keyed copy of itself rather than the legacy DOM version's remove-class/force-reflow/re-add-class dance: a newly created element always starts its CSS animations from the beginning, in every browser, so this is both simpler and more robust than replaying that dance against a node the legacy version kept reusing.

- The shared `<symbol>`/`<use>` sprite sheet (the minds mark, the laptop, the miniature app interface) is now a small component mounted once, since Mithril's icon catalog had no prior precedent for a shared, referenced-by-id sprite sheet -- everywhere else inlines its glyph directly.

- `GET /ui/api/create/attempts/<id>` now carries `is_remote`, `expected_duration_seconds`, and `onboarding_services` (the app-cloud icons, pre-inlined) alongside the existing live-attempt fields, so the walkthrough (which the Mithril tranche work had shipped as a bare progress view, deferred pending this port) can read the same context the legacy page's render call did.

- The legacy JinjaX page, its static JS, and its own CSS section are unaffected by the port and still exist -- they are dead-routed (every page path now serves the SPA) but still exercised by `templates_test.py` and the visual-diff harness, which the Mithril migration has not yet removed.
