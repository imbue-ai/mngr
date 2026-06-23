Hardened workspace post-create setup against slow `mngr` invocations:

- Bumped the onboarding permissions-preference `mngr exec` timeout from 30s to 60s, so writing the Q3 permissions preference into the workspace doesn't time out (and abort with exit -15) when host-side `mngr` is slow (e.g. under heavy load or cold provider discovery).

- Added debug logging around each `mngr imbue_cloud …` subprocess (subcommand, elapsed time, returncode, and whether it timed out) so a slow or timed-out post-create operation (Cloudflare tunnel create, backup bucket create, etc.) is attributable instead of surfacing only as a bare "exit -15".
