# Authentication

Understanding this behavior corpus calls for the tmr-behaviors skill; consult it when reading this file.

This area covers the bare origin's own surface: signing in with a one-time code, the session that sign-in establishes, the pre-authorized paths an embedding host uses to skip the code flow, the bare-origin home page, and the goto bridge that carries one session onto every agent origin.
The Rules in the corpus root's `invariants.feature` bind all of it.

Two lifetimes matter throughout this area and are easy to confuse.
One-time codes live only in the proxy process's memory: a restart mints a fresh code and forgets every code the previous run issued, spent or not, so a code from a previous run can never re-authenticate a stale tab.
Sessions outlive the process: the cookie-signing key is persisted in the proxy's state directory, so a cookie issued before a restart still verifies afterward.

An embedding host's own browser authorization -- the sign-in it runs on its own surface -- is a bordering system, specified from that system's perspective in its own corpus.
Each corpus states the shared constraints from its own surface; neither defers to the other.

## Out of scope

- How the login URL reaches the user beyond "printed to the proxy's terminal": the stdout `login_url` event belongs to the planned stream area.
- The embedding host's side of the pre-authorized paths: how it generates a preauth value or a browser-bridge token, and how it pre-sets or presents them.
- The `frame-ancestors` policy the proxy appends to agent responses for an embedding host, which gates framing rather than the session (touched in `forwarding/`).
