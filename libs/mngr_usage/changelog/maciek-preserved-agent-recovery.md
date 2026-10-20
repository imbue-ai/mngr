Usage preservation now shares mngr's host-aware archive destination selection, preventing known cross-host agent identity collisions from overwriting an existing preserved archive.

New usage archives record identity and copy outcomes in the shared core manifest instead of a separate usage metadata sidecar. Usage and usage-wait discovery read the manifest, retain legacy archive support, and continue including preserved spend by default.

Destroying an agent no longer walks the host's agent listing to resolve its provider, so usage preservation costs one fewer remote round trip per destroy. A preserved `data.json` that names no usable agent is now reported rather than silently dropped, which would otherwise leave that agent's spend out of `mngr usage` with no explanation.
