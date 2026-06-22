Added `specs/bare-providers/` (spec.md + concise.md): a design proposal for
running agents directly on a cloud VM with no Docker container, as a second
config-selected shape of the aws/gcp/azure providers. Introduces a
substrate-x-realizer architecture (a `HostRealizer` seam injected like the existing
`VpsClient`) so "with Docker" vs "without Docker" becomes a reusable axis rather
than a per-cloud class matrix, with a staged rollout that later folds
local/docker/lima/ssh into the same grid. Also adds `specs/uncertainties.md` noting
that the bare mode supersedes the "single mode of operation" framing in
`specs/vps-docker-provider/spec.md`, and `specs/bare-providers/extraction_design.md`
giving the implementation-level `HostRealizer` seam contract, state-ownership
split, host-record evolution, and per-method migration for Stage 1.

Updated the root pytest coverage config to track the renamed `imbue.mngr_vps`
package (was `imbue.mngr_vps_docker`).
