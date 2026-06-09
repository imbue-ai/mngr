Documented (in a code comment) why the per-agent config sync deliberately omits
pi's `npm` dir: pi auto-installs the `packages` listed in the synced
`settings.json` into each agent's `$PI_CODING_AGENT_DIR/npm` on startup, so
npm-package extensions (e.g. `npm:pi-subagents`) are available under mngr without
copying `node_modules`, at the cost of a ~1s per-agent install that needs network
on first launch. No behavior change.
