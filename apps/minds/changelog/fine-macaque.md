- The start flow's comparison table now labels the second column "Custom setup" and gives it an "Advanced" badge, matching the "Recommended" badge on Imbue Cloud. The badge takes its color from the column: accent for the recommended one, grayscale for the rest, so the two do not read as competing recommendations.

- Modal backdrops now dim the titlebar too, instead of stopping at the 38px strip and leaving it bright above the dimmed page. This covers the custom-create and sign-in modals along with every other one.

- Modal content that overflows is now clipped by the card's own bottom edge rather than by its padding. A long form previously cut off a padding's width above the bottom, leaving a band of blank card beneath a half-sliced Create button.

- Modals are 72px shorter, 36px off the top and bottom, so a tall one no longer stretches the window's full height.

- The sign-in modal is wider, and its fallback link sits on one line (truncated, click-to-copy) instead of wrapping over several -- the same treatment the workspace share link already had.

- The "Change answer" tooltip on a start-flow answer now opens above the button rather than below it. Tooltips can request a side via `data-tooltip-placement`, and still flip to the other side when the preferred one has no room.

- Clicking "Change answer" now leaves 16px of clearance under the last turn when the transcript auto-scrolls, rather than parking the button flush against the bottom edge.

- The "Custom setup" name now carries through the whole start flow: the answer button, what the transcript records you as having said, the question that introduces the two options, and the follow-up when that dialog is closed unfinished.

- The comparison table uses the check marks themselves rather than icon-set glyphs -- a filled mark for Imbue Cloud, a plain one for Custom setup.

- A manifesto point's marker is a chevron that turns as the point opens, and it takes its color from the row: primary alongside the label, and accent with the label on hover, instead of sitting a shade apart in tertiary. The marker holds a fixed-width column, so an open point's explanation starts flush under its label rather than a few pixels further in, and the explanation keeps a gap under it so it does not sit against the next point's label.

- The markers now fade in with their own line as the list streams, rather than all standing there at once in front of rows whose labels have yet to type themselves in.

- An open manifesto point sets its label bold and its explanation in primary.

- Fixed opening a manifesto point part-way up the transcript scrolling the column to the end, which threw the reader off the row they had just opened. Auto-scroll now follows the number of turns -- a turn arriving or being undone -- rather than the anchor's layout position, which a disclosure also moves.

- Tightened the start flow's turn spacing from 48px to 40px, and the quieter answer button is now a bordered secondary rather than a borderless ghost. Its label stays primary -- only its weight is lighter -- so the border does not make it read as disabled.

- A create started from the start flow's Imbue Cloud answer now restates itself as "Create on Imbue Cloud" rather than listing the seven settings behind it. That answer submits the remote preset without ever showing the form, so reading its region, backup, template and branch back made one choice look like several. Every other create still restates each setting, including one that picks Imbue Cloud from inside the form -- there the settings are yours. Reloading mid-create falls back to the settings list, along with the rest of the transcript.

- Signing in, creating an account, and verifying your email now bring the app back to the front on their own. All three finish in your browser, which takes focus with it, so the app used to sit behind it having already moved on -- the only way back was the "Open app" link on the browser's success page. The app now raises itself as soon as it sees the sign-in land or the address come back verified. This existed before sign-up moved to the hosted browser page and was lost in that move; the "Open app" link stays as the fallback for when the app is not running.

- The start flow's aside now reads "I already have a workspace (log in)" rather than "I already have one", and the account step's quieter answer is "Sign in" -- each naming the thing you already have at the point it is offered.

- An answer row coming to rest now leaves 32px under it instead of 16px, so a new set of buttons does not sit almost against the window's bottom edge.

- A transcript turn now breaks a long unbreakable token instead of running out past its bubble. A create summary restating a local repository path -- which has no space to wrap at -- overflowed the grey pill and carried on across the page.

- Fixed the app not coming back to the front when you sign in by picking an account the browser already had signed in. That path is the fastest one, so the account often landed on the app's own channel before the sign-in poller had run even once -- and dismissing the wait stopped the poller that was going to raise the window. The signal now comes from whichever gets there first.

- The first-launch intro's two lines now fade in whole rather than typing themselves out a character at a time. Each line arrives on the same curve the lockup resolves on, the italic words come into focus instead of only brightening, and a line blurs as it rises away -- so the type reads as the same gesture as the mark above it rather than as a different animation underneath it. The film runs about a second longer as a result: 8.0s from launch to the parked mark, against 6.9s before.

- The brand color is now the app's success green (`#0c8106`) rather than the blue it was. It carries the lockup -- in the first-launch intro, in the titlebar the intro parks it in, and in the start route's titlebar the app takes over with -- along with the intro's two serif lines.
