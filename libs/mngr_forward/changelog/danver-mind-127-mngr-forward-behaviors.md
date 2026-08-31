Added the initial `mngr forward` behavior corpus at `libs/mngr_forward/behaviors/` -- the proxy + auth core -- authored in the repo's behavior language and guarded by a live-corpus test (`test_behavior_corpus.py`).

The corpus speaks in host/agent terms: the proxy's addressable unit is the *agent*, served at its `agent-<hex>.localhost` origin, and a *host* is the substrate an agent runs on (the tunnel target, loopback, and the legacy `host-<hex>` coordinate that only ever redirects).

The corpus root carries the cross-cutting invariants: single-use codes, fetch-never-spends, no agent data or backend content without a session, unforgeable and bounded sessions, a single credential, the session cookie never reaching backend code, and no open redirects.

`authentication/` covers one-time-code sign-in, session lifetime and integrity, the two pre-authorized paths an embedding host uses (a preauth cookie and a browser-bridge token, both startup-fixed and reusable), the bare-origin home page (an index of discovered agents), and the goto bridge -- token audience, expiry, and forgery; one hop covering an agent's default service and every service origin through the domain-scoped cookie; coordinate canonicalization; and self-healing direct navigation.

`forwarding/` covers host-header routing across the `[<service>.]agent-<hex>.localhost` origins (plus the default-service redirect and the legacy host-coordinate redirect), HTTP byte-forwarding fidelity, WebSocket forwarding (relay, the post-accept close code, handshake refusal for anything unforwardable, and the no-client-headers invariant), backend error behavior (503 before headers, 502 after, 504 for a wedged or pool-exhausted proxy, and the self-healing loading page), and the host-loopback refusal invariant.

Also hardens startup: the proxy now loads its cookie-signing key eagerly, so a corrupt or unreadable key file fails the process at boot rather than erroring on the first request that needs it.

Deliberately out of scope here and tracked separately: the stdout envelope stream, discovery and backend resolution, and reverse tunnels.

Each folder's prose now lives in a `README.md` opening with the mandated incipit, replacing the `overview.md` files: `overview` is no longer a reserved basename, so such a file is read as a sidecar and rejected for having no matching `.feature`.
