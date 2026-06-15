Sped up in-app `mngr` invocations (e.g. the `mngr message` sent when a permission request is approved or denied) by adding `MngrCaller`, which runs the `mngr` CLI in a child forked from a pre-warmed `multiprocessing` forkserver instead of spawning a fresh Python interpreter each time.

The forkserver imports `imbue.mngr.main` once at app startup (on a background thread, off the request path), so subsequent calls skip the multi-second interpreter-and-plugin import cost. Running in a forked child also keeps `mngr`'s global-state changes (loguru, `sys.argv`, stdout/stderr) out of the long-lived backend process.

`MngrMessageSender` now always routes through this caller (defaulting to the shared, pre-warmed instance), so approving or denying a permission request no longer spawns a fresh `mngr` process. Other direct `mngr` CLI call sites can migrate onto `MngrCaller` incrementally.

Approving or denying a permission request (both file-sharing and predefined-credential dialogs) no longer blocks on the agent nudge: `MngrMessageSender.send` now dispatches the `mngr message` onto a background thread tracked by the app's concurrency group and returns immediately, so the dialog responds without waiting for the (network-bound) delivery to complete.
