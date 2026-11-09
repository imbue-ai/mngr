Workspace settings gains a Keyboard shortcuts group. It shows the workspace's own keyboard shortcuts page (which the workspace serves at `/keyboard-shortcuts`) in a frame that blends into the pane, so a workspace's shortcuts can be looked at and changed from the desktop app's settings. The workspace's own "Keyboard shortcuts..." menu item now opens this group too, which shows where the shortcuts live.

Three additions to the embed contract (v15) carry this:

- `minds:keyboard-shortcuts-page` (workspace to embedder) reports the page's height and whether its recorder is listening. The frame takes the height, so the page scrolls with the rest of the pane. While the recorder listens, Escape no longer closes the settings panel, so it can be recorded, and the close-tab shortcut no longer closes a workspace window.

- `minds:open-keyboard-shortcuts` (workspace to embedder) asks the chrome to open the Keyboard shortcuts group. The chrome announces that it takes it with the new optional `opensKeyboardShortcuts` field on `minds:embedder-capabilities`; a workspace that never hears it keeps opening its own dialog.

- The frame is sandboxed like the main workspace frame, and stays hidden until the page's first report. A workspace that never sends one, because it is too old to serve the page or is not running, gets a notice with a button to the Updates group instead of a frame showing its whole desktop.
