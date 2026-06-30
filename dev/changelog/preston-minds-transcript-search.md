Added blueprint planning docs (`blueprint/find-past-agent-transcripts/`): the design plan and a manual-test plan for letting minds agents discover and read past agents' preserved transcripts.

Added a `just minds-env-copy <from> <to>` recipe that creates a new local dev minds env by copying an existing env's client config into a fresh data root (no `minds env deploy` / cloud provisioning), for spinning up a per-feature env to test branches.
