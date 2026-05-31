Fixed a bug where a nested `claude -p` (or any nested `claude`) run from inside
a mngr-managed Claude agent's session would be adopted into the parent agent:
the child session inherited `MAIN_CLAUDE_SESSION_ID`, so mngr's readiness and
transcript hooks (all guarded on that variable) fired for it -- leaking the
child's messages into the parent's transcript and flapping the parent's
lifecycle-state files and session tracking.

Each Claude agent now provisions a small `claude` shim onto its session PATH
that strips `MAIN_CLAUDE_SESSION_ID` from nested invocations before exec-ing the
real binary. The main agent launch resolves and runs the real binary directly,
so its environment -- and therefore `/clear`, `/compact`, and session resume --
are unaffected.
