- `ShareInfo` gains `media_port`: the relay TCP port the connector allocated to the share for the Screen's media proxy (`specs/screen-studio-integration/spec.md`, section 6), None against a connector that predates the field or for a share not yet re-shared since it exists.

Imbue Cloud slices publish the ports a create declares (`specs/published-ports/spec.md`, section 6).

- The carve reserves one box port per distinct declared guest port beside the two sshd ports (checking TCP and UDP listeners and every recorded env file), the slice's env file records them as `MNGR_SLICE_PUBLISHED_PORTS`, the root helper DNATs each entry with its declared protocol in both NAT chains, and the container is run with `-p 0.0.0.0:<guest>:<guest>/<protocol>` so the VM-side port equals the guest port. The reserve marker lines carry the published ports after the ordinal; the parsers accept the old three-field form.

- The connector wire types (`LeaseResult`, `WorkspaceInfo`, `LeasedHostInfo`) gain an additive `published_ports` list, the lease request carries the create's declarations, and the slow-path rebuild republishes the lease's ports inside the VM.

- An `ImbueCloudHost` answers `get_published_endpoints()` from the current lease record (re-read like the SSH ports, so a restore at fresh box ports is picked up), `mngr list` carries them as `host.published_endpoints`, and adoption records them beside the SSH endpoints. A workspace on a host baked without published ports reports none.

- `SliceBakeOutcome` reports the baked slice's published ports.
