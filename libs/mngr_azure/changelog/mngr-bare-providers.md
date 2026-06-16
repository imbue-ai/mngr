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
