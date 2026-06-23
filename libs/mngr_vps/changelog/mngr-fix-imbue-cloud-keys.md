SSH host keys are now unique per host. Every VPS-backed host (AWS, GCP, Azure, OVH, Vultr, and imbue_cloud slices) gets its own freshly-generated VPS/VM-root and container sshd host keypair at create time, stored under `<key_dir>/host_keys/<host_id>/`, instead of one host keypair shared across every host a provider instance created. This removes the risk of one host's key being reused to impersonate another. The per-host keys are removed when the host is destroyed.

`mngr create --format json` surfaces the host's baked sshd host public keys (VPS/VM-root and container) via a new `get_ssh_host_public_keys` provider method, so pool-bake tooling can persist and pin them instead of scanning the host after creation.

Existing hosts created before this change keep working: the offline pause/resume path falls back to the legacy provider-global host key when a host has no per-host key recorded.
