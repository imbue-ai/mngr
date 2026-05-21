## AWS provider support: root-level changes

- `mngr create` CLI markdown docs regenerated to include the new AWS provider's build-args help (removes the dropped Vultr/OVH `--vps-os=` line at the same time).
- Top-level coverage configuration adds `--cov=imbue.mngr_aws` so the new package contributes coverage data.
