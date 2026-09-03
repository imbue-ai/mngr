The alpha release channel now serves minds 0.5.0 (ToDesktop build `260902shwco3ynx`), replacing 0.4.2. Stable and beta stay on 0.4.2.

0.5.0 is a minor bump because the workspace changed shape: signing in is a provider chooser over named provider accounts rather than a single Claude login modal, a workspace no longer creates a chat at boot -- it opens on its new-tab screen and the first chat is one you start on the account you picked -- and harness availability follows from the accounts you signed in to rather than from host feature flags. Outside the workspace, terminal panes no longer steal focus on websocket reconnect, and `mngr list` no longer stalls on one slow host.

Production pool hosts are baked at `minds-v0.5.0` (15 slices, 7 US-EAST-VA and 8 US-WEST-OR), so a 0.5.0 create leases a pre-baked machine rather than rebuilding one.

The connector's download fallback is deliberately untouched: it is what the public download link serves while the update feed is unreadable, and it tracks `stable`.
