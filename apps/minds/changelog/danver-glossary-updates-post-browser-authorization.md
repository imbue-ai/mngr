Added workspace-glossary terms grounding the minds desktop client's browser authorization.

Defined *browser authorization component* -- the browser-facing part of the desktop client (the bare-origin web UI served by `minds run`) -- distinguishing it from the desktop client's other duties (agent/workspace creation, reverse proxying to workspaces).

Defined *session* -- the authenticated state of a browser connected to the browser authorization component, carried by the *session cookie*: an HTTP cookie whose value is a token signed with the installation's session-signing key, so a tampered cookie, one minted under another installation, or one older than 30 days is rejected.
It is the sole credential gating every page the component serves, covers all of the user's workspaces, and is distinct from the optional imbue-cloud account sign-in.

Defined *installation* -- one copy of the desktop client's local state (a single data directory, e.g. `~/.minds`), which scopes the one-time code, session-signing key, sessions, and error-reporting consent.
