# Authentication

Component: the bare-origin surface of the forward proxy -- sign-in with a
one-time code, the session it establishes, the home page -- plus the goto
bridge that carries that one session onto every workspace origin.

The features in this folder cover sign-in (`signin`), session lifetime and
integrity (`session`), the pre-authorized path an embedding host uses to
skip the code flow (`preauth`), the bare-origin home page (`landing`), and
the bridge (`goto-bridge`). The Rules in the corpus root's
`invariants.feature` bind all of them.

Two lifetimes matter throughout this area and are easy to confuse:

- One-time codes live only in the proxy process's memory. A restart mints a
  fresh code and forgets every code the previous run issued, spent or not,
  so a code from a previous run can never re-authenticate a stale tab.
- Sessions outlive the process. The cookie-signing key is persisted in the
  proxy's state directory, so cookies issued before a restart continue to
  verify afterward.

The minds corpus (`apps/minds/specs/authentication/`) describes bordering
territory -- the minds desktop client's own sign-in and its use of this
proxy's bridge -- from that system's surface. Each corpus states shared
constraints from its own perspective; neither defers to the other.

## Out of scope

- How the login URL reaches the user beyond "printed to the proxy's
  terminal" (the stdout `login_url` event belongs to the planned `stream/`
  area).
- The embedding host's side of preauth: how it generates the value,
  pre-sets it in its browser shell, or passes it to the proxy.
- The Secure cookie attribute under the TLS serving mode (see the corpus
  overview).
