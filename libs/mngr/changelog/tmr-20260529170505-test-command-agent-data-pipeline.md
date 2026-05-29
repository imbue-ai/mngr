Fixed the "run a long-running data pipeline" example in the mega tutorial: it
used `--idle-mode run --idle-timeout 60` without a provider, which is rejected
because idle detection is only supported on remote providers. The example now
uses `--provider modal` (consistent with the neighboring batch-job example).
Also strengthened the corresponding e2e release test to verify the agent's
provider, idle settings, command, and that the pipeline process is actually
running on the host.
