Deflake two Modal acceptance tests.

The discover-hosts test polls discovery for up to 150s after host creation instead of asserting on a single snapshot: Modal's sandbox listing is eventually consistent, and CI observed it stay stale for over a minute after a successful create.

The graceful-stop restart test is marked flaky (CI observed the restarted sandbox's fresh SSH tunnel transiently refuse connections, an infrastructure failure the test cannot control), and its accidentally duplicated acceptance/timeout decorator stack is collapsed to one.
