The desktop app's overlay modals (workspace menu, inbox, help, sign-in) are now rendered as in-page DOM instead of iframes. Opening a modal no longer mounts a nested iframe: the always-warm overlay host fetches the modal's markup as a bare fragment (`?fragment=1`) and injects it directly, and its per-modal JS runs as a module in the host. Behavior is unchanged.

This removes a layer of iframe machinery from the overlay surface -- the per-frame IPC fan-out, the SSE priming handshake, the `nodeIntegrationInSubFrames` subframe bridge, and the front/back-buffer iframe swap. The SSE-driven modals (workspace menu and inbox) now read the live workspace list / request state from a single cache the overlay host keeps, primed on load and kept current as events arrive.

Each modal's JavaScript is single-sourced in one module that drives both the desktop app's injected fragment and the modal's standalone full page (the browser/dev path), so there is no duplicated per-modal script.
