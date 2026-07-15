# Tighten the filtered-hosts exec tutorial e2e test

- Changed: `test_tips_exec_filtered_hosts` now distinctly verifies both effects its scope names -- it asserts the `echo $MNGR_AGENT_ID` half produces a standalone output line equal to the host's id, in addition to confirming the `env | sort` dump contains that host's `MNGR_AGENT_ID=` line. The previous id check was redundant with the env-dump assertion and did not observe the echo separately.
