Reworked how workspace accent colors interact with the minds app shell:

- Non-workspace minds screens (Home, Create, accounts, inbox, auth, ...) and the startup/quitting/error loading screen now paint a pure-white neutral background instead of the previous light-gray / dark chrome. (Light-mode only for now; a pure-black dark-mode variant is a deferred follow-up.)

- The titlebar now shows the neutral white chrome on those general screens and only adopts a workspace's accent color while you're on a workspace-scoped screen -- the workspace itself plus its settings, sharing, destroying, and recovery screens. Previously the titlebar kept the last-opened workspace's accent even after navigating away to a general screen.

- Removed pure black and pure white from the workspace color swatches, so a workspace's accent can no longer be indistinguishable from the neutral chrome. You can still type either value into the workspace-settings hex input if you want it.

- Cancelling out of the Create form now clears the previewed color from the titlebar and returns it to the neutral chrome, instead of leaving the previewed color stranded on the bar.
