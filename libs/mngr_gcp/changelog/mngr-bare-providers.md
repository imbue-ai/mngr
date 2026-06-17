Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename: the VPS
provider is no longer Docker-only, so the package and its shape-agnostic base
classes dropped "Docker" from their names (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerHostStore` -> `VpsHostStore`, `VpsDockerError` ->
`VpsError`). Import-only change; no behavior difference.


Enabled bare placement (`isolation=NONE`): the idle agent runs `shutdown -P now`
as the VM's root, which on GCE stops the instance, so the container-only sentinel +
host-side systemd watcher is skipped for bare.

Added bare-placement (`isolation=NONE`) release tests, and fixed a resume bug they
caught: `start_host` read the host record via the Docker volume, which a bare host
does not have, so it now resolves the store through the realizer.

``stop_host`` / ``start_host`` moved to the shared base ``OfflineCapableVpsProvider``; GCP now supplies only the GCE ``_pause_cloud_instance`` / ``_resume_cloud_instance`` hooks. Behavior-preserving.

Moved mngr host identity (host id and created-at) out of GCE *labels* and into instance *metadata*, joining the host name and per-agent records already kept there. Only ``mngr-provider`` remains a label, because it is the server-side ``instances.list`` discovery filter. Host id is now stored verbatim and created-at as an ISO-8601 timestamp (no more GCE-charset lowercasing / ``%Y-%m-%dt%H-%M-%S`` encoding). Backward-incompatibility: a GCE instance created before this change carries its host id / created-at only in labels, so an *already-running* pre-upgrade host will no longer resolve by id for offline discovery / ``mngr start`` and its reconstructed created-at falls back to now(); destroy and recreate such hosts (online hosts reachable over SSH are unaffected -- they resolve via the on-volume records).
