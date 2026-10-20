# Share button deep link (embedder side)

The embed contract gains a workspace -> embedder message type,
`minds:open-share-settings` (contract version 3): clicking Share on an app
inside a workspace now opens the minds shell's workspace-options panel on its
Share tab, focused on that app, instead of the workspace showing an
instructional popup. The workspace iframe stays mounted; the panel floats over
it like the other option panes.

The options panel also applies a share-target deep link whenever the target
parameter's value changes, not only on first load, so a deep link that arrives
while the panel is already open still selects the right app.

The sharing surfaces now use the standard lucide user-plus icon (the titlebar
Share tab and the options overlay strip), replacing the custom person glyph.

The Share tab's per-app entries now wear each app's own icon: the SVG the app
registered in the workspace (carried through the services event log and
sanitized before it is inlined in the shell), or a per-app monogram when it
registered none -- matching how the workspace itself draws its apps. An older
workspace that predates icon-carrying service events falls back to monograms.
