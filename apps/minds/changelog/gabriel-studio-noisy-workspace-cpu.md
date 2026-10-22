Imbue Studio no longer pins a CPU core when a workspace keeps re-announcing its apps.

- An update to the backend's view of workspaces that changes nothing -- a workspace re-announcing services it already announced, a discovered-agents snapshot identical to the last one -- no longer wakes the backend's listeners (the UI channel, the update detector, latchkey auto-register and the rest). A workspace still running the retired `app-watcher` re-announces every app every few seconds, and replaying its multi-gigabyte services log used to trigger a full UI re-derive per line.

- The output of `mngr forward` is read through a buffer instead of one byte per system call.

- Each UI update reads the pending permission requests from the latchkey gateway once, shared by the inbox badge and the notification feed, instead of twice.

- Requests to the latchkey gateway reuse one TLS context per client instead of parsing the certificate bundle every time a client is built.

- UI updates are spaced at least 100 ms apart. A change arriving inside that gap is folded into the next update, not dropped.
