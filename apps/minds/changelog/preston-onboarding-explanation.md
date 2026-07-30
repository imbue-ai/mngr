The workspace-creation loading page now walks you through minds while the workspace is created.

Top strip (always visible): a "Setting up your machine" title above the progress bar, the live stage caption, and the collapsible logs. The bar runs no wider than the illustration below it. The workspace is entered the moment it is ready, so there is no Begin button and the bar needs no separate finished colour.

The stage caption and the details toggle appear once, in that strip. They had been showing twice -- again at the foot of the page, left and right -- because the walkthrough's strip and the page's original footer both carried them. Only the strip's pair was ever wired up, so the caption at the foot never advanced past the stage the page was first served with, and its toggle did nothing at all.

Opening the details no longer pushes the page into a scroll. The logs take a fifth of the window, which the walkthrough at full size has no room for, so while they are open the walkthrough compacts: the illustration scales down and the spacing around it closes up, making room where the logs need it. The panel's height is a share of the window rather than a fixed 200px, since how much room there is to make depends on how tall the window is.

The walkthrough plays itself: eight steps, held for 7 seconds each (10 for the chat, 9 for publishing, and 16 for the permissions step, which plays a longer sequence), with nothing to press to begin. Below the illustration sits a row of dots, one per step, each in a fixed-width slot so nothing shifts: the current step's dot stretches into a pill whose fill runs out as the step does. Any dot can be clicked to jump to its step.

Each step's copy is set at 1rem rather than on the type scale, so the walkthrough's message is not the smallest text on the page.

Steps:

- "This is Minds: your machine for building personalized apps. / Learn more while you wait.", over the minds mark.

- "Agents can build any interface you can imagine.", over a chat: the request types itself out, backspaces and tries another thing to build -- plants, then email, then the workout tracker, which it stays on, since that is the app the next step opens while the agent's "You've got it!" sits underneath. Advancing carries the exchange over: the same bubbles are what the window's chat pane contains, so nothing has to travel or line up -- the frame simply draws itself around them, border and title bar arriving while the bubbles settle from a shade larger.

- "Agents and tools run in tabs, so you can see everything at once.", over a workspace window that opens a second tab -- the tab already open gets a beat to be read, then an oversized pointer glides to the "+", clicks it with a splash ring, and an app tab appears. The window carries on the conversation from the previous step -- always the workout tracker, which is what the chat step leads with -- and the tab that opens shows the app built from it, a month with the days trained filled in.

- "Agents can get data from other apps you use, like Slack or Notion, and browse the web to complete tasks.", over a cloud spinning app icons from the bundled latchkey services catalog, inlined into the page so they appear with it.

- "Your agents can only perform actions you approve." with a Learn more link beneath it, over a scene that plays out: a dashed boundary sits between the machine and the cloud from the start, and a permission request waits on the left with Deny and Approve on it. A pointer crosses and approves; the green button holds a moment, then travels over and settles as a link through the boundary, which carries light pulses up and down as live traffic. The pointer then crosses back to Deny, which travels the same way and lands on the link as an X, closing it.

- "Share access with your teammates, friends, or even your phone.", over a laptop with an arrow drawing across to a phone, where the laptop's interface then appears. The second line depends on where the machine runs: a cloud machine can be reached with the laptop closed, a local one only while this computer is on.

- "You can also publish your apps, or adapt what others have made.", over two identical machines either side of a cloud. The app inside each is tinted differently, so what differs reads as the app rather than the computer; the story runs in order: an arrow draws up to the cloud and only then does the published copy appear there, then an arrow draws down and only then does their version appear. Both arrows stay once drawn.

- A closing step for the rotating tips, on a screen of their own with no illustration: a large "Hang tight — your machine is nearly ready." takes the graphic's place.

Anything in the walkthrough can be hovered for a short explanation: the minds mark, each demo tab and pane, the new-tab button, the app cloud, and the credential-protection line.

Workspace color selection is unchanged: the create form's auto-chosen hidden color is used as before (no picker anywhere in this flow).

The page no longer scrolls: it was sized to a full viewport height on top of the titlebar's 38px offset, so it always overflowed by exactly the titlebar's height and could be scrolled with nothing to scroll to. It is now sized to the region below the titlebar and sits still; windows too short to fit the nav can still scroll the content area so the buttons stay reachable.

The tips change every 7 seconds, and the rotation starts when the last step is reached rather than at page load, so the first tip gets its full turn instead of being swapped out moments after it appears.

The rotating tips on the last step say what you can do rather than where to click -- running several agents at once, running them in the background or on a schedule, sharing a machine or a single app, viewing and revoking permission, keeping several machines, backups, stopping and restarting a machine, and reporting a bug. Naming menus and labels would go stale as soon as one moved.

The pictures are drawn to one system. Each lives in its own viewBox at its own size, so a stroke declared once came out at a different thickness in each -- the cloud read at 1.5px beside a laptop at 4.2px, and the same laptop was heavier on one step than another. Every outline is now 2.5px and every inner detail 1.5px, held there by non-scaling-stroke so the weight does not follow the scale. Arrowheads are filled rather than stroked, so nothing overlaps a line end or doubles up where a semi-transparent cap crosses one. The clouds were also drawn to a viewBox tight against their own path, which clipped away half the outline at the top, bottom and sides (the "extra thin" edges), and were stretched out of their proportions; they now have room for their stroke and keep their shape.

The drawings are inked in a flat gray rather than the theme's tertiary text and border tokens, which are translucent: a line crossing another compounded their alpha, so a join read darker than either line, which is what made the arrowheads look stuck on top of their lines.
