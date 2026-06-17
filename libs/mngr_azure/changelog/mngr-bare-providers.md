Renamed the `_AGENT_TAG_FIELDS` constant imported from `mngr_vps` to the
public `AGENT_TAG_FIELDS` (matching its sibling `AGENT_TAG_PREFIX`), so the
Azure tag-mirror code no longer imports a private name across modules. No
behavior change.


Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename and the
accompanying class renames (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerError` -> `VpsError`, etc.). Import-only; no behavior
change.


Enabled bare placement (`isolation=NONE`): an Azure OS shutdown does not halt
compute billing, so the bare agent's idle `shutdown.sh` runs the ARM
self-deallocate directly (the same call the container idle watcher uses), keeping
the self-deallocate role assignment and skipping the host-side sentinel watcher.

Added bare-placement (`isolation=NONE`) release tests, and fixed a resume bug they
caught: `start_host` read the host record via the Docker volume, which a bare host
does not have, so it now resolves the store through the realizer.

Bugfix: `mngr start` of a deallocated Azure host now re-mirrors the resumed host
record to the external (Blob bucket) store, so offline / `mngr list` reads no
longer report a just-resumed Azure VM as STOPPED until the next mirroring write.

``stop_host`` / ``start_host`` moved to the shared base ``OfflineCapableVpsProvider``; Azure now supplies only the deallocate/start hooks plus the static-IP known_hosts rebind no-ops. The shared base is what now guarantees the resume-mirror above happens on every provider.

Updated the host_dir sync to call the realizer's `host_dir_path_on_outer`
directly after the redundant `_host_dir_path_on_outer` forwarder was removed
from the shared VPS provider. No behavior change.

The idle-watcher install, the host_dir-to-bucket sync daemon install/before-pause, and the best-effort `_on_host_finalized` step runner all moved to the shared `OfflineCapableVpsProvider`. Azure now supplies only its hooks: the `Azure VM` display name; the ARM self-deallocate `.service` body and the curl-plus-deallocate-script outer prep (`_idle_watcher_service_unit` / `_prepare_idle_watcher_outer`); the bare-placement self-deallocate `shutdown.sh` (`_write_bare_idle_shutdown_script`); the sync gate (config flag + bucket + managed identity present), azcopy install, `azcopy sync` `.service` body, and blob target URL; and the self-deallocate role assignment prepended via `_post_finalize_steps`. The host-side systemd unit names changed from `mngr-azure-idle-watcher` / `mngr-azure-host-dir-sync` to the shared `mngr-idle-watcher` / `mngr-host-dir-sync`. Behavior-preserving otherwise.

Updated the VPS build-arg parsing imports to point at the new `imbue.mngr_vps.build_args` module (moved out of `imbue.mngr_vps.instance`). Import-only change; no behavior difference.
