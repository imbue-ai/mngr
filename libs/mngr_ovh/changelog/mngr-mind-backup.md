Added `inotify-tools` and `jq` to `_REQUIRED_OUTER_PACKAGES` so the new
`snapshot_helper.service` provisioned by `mngr_vps_docker` has the tools
it needs on OVH-leased outers (the cloud-init path on Vultr / generic
VPSes pulls these in via the cloud-init `packages:` list).
