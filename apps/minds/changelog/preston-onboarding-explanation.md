The workspace-creation loading page now shows an onboarding walkthrough while the workspace is created: nine one-sentence steps with Previous/Next, over a shared scene graphic that zooms out from the minds icon to the full computer-tunnel-workspace picture.

- minds phase: what minds is, how workspace tabs work (a demo tab-space that the Next button walks through: chat, apps, web -- with the user's message on the right and the agent's on the left), and a theme-color picker that restyles the demo live and sets the actual workspace color.

- latchkey phase: credentials are encrypted and stored locally, never visible to agents, plus a scrolling carousel of the services latchkey can connect to (sourced from the bundled latchkey services catalog, with vendored simple-icons brand icons). The latchkey tile is a redraw of the real brand mark.

- full picture: your computer connecting over an encrypted tunnel to the workspace machine, with copy adapting to local vs cloud workspaces.

The progress bar, stage caption, logs, rotating tips, and any creation failure stay hidden until the user reaches the last step. There, the workspace tile fills in lockstep with the progress bar, its caption reads "setting up..." and flips to a glowing green "Ready" when creation finishes, and a pulsing Begin button (replacing the old auto-redirect) plays a zoom-in animation and enters the workspace. Clicking any scene icon jumps back to the step that explains it.

The walkthrough is now fully integrated with the create flow:

- It auto-shows only on the user's first-ever workspace creation (persisted flag). Later creations show the plain loading screen (progress bar visible immediately, auto-redirect on ready) with a "Learn more about Minds" button that reopens the walkthrough on demand.

- The walkthrough's theme-color pick now actually changes the workspace's color: a new create-operation color endpoint records the pick, which is applied as the workspace's color label during creation (or immediately if creation already finished).

- The create form shows a visible theme-color swatch picker (pre-selected with the first unused palette entry) once onboarding has been seen; on the very first creation the picker is hidden there, since the walkthrough owns the color pick.
