Added offline ``host_dir`` support for the GCP provider, matching the AWS / Azure shape. A stopped GCE instance's ``host_dir`` is now readable without starting it (so ``mngr event`` / ``mngr transcript`` / ``mngr file`` work against it), captured operator-side at ``mngr stop`` and uploaded to a Google Cloud Storage state bucket. Host + agent records still live in GCE instance metadata (where they already fit comfortably and need no prepare step).

``mngr gcp prepare`` now also creates a GCS state bucket (named ``mngr-state-<project_id>`` by default, configurable via ``[providers.gcp] state_bucket_name``). ``mngr gcp cleanup`` now deletes that bucket alongside the firewall rule, with a new ``--force`` flag that opts into deleting it even when it still holds offline host state from hosts no longer present as instances.

New config fields on ``GcpProviderConfig``: ``state_bucket_name`` (overrides the derived name) and ``is_offline_host_dir_enabled`` (default on; set to ``False`` to turn the feature off without removing the bucket).

The shared provider release harness's Trip 1 opt-in offline-host_dir step (gated by ``MNGR_RELEASE_TEST_OFFLINE_HOST_DIR=1``) now runs against GCP too, asserting that a stopped host's marker file is served from the offline mirror via ``mngr file get`` without resuming the host.
