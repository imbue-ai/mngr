Merge main into the self-hosted sharing branch (no further `mngr_forward` changes beyond the sibling entries in this PR).

Review fixes on top: the debug index no longer renders broken `/goto//` links for agents with no known host, and a discovery-invariant violation during agent setup now logs a warning instead of failing silently.

Second review pass: `/goto/` host ids are validated against the strict `host-<32hex>` shape and lowercased (malformed ids could inject bytes into the redirect and uppercase ids dead-ended in a 403); the per-SNI TLS cert cache evicts its oldest entry at the cap instead of permanently serving the static cert to new origins; and the temp-PEM helper no longer leaks a file descriptor when the write fails.

Third review pass: the /goto/ bridge routes deeper sub-origin labels with the hostname-label charset (only the last label is a service name), so a deep sub-origin like `a--b.svc` completes login instead of 404ing.
