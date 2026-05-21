End-to-end fixes for the OVH-backed imbue_cloud pool flow that surfaced
while smoke-testing the bake / lease / first-start sequence against a fresh
dev env.

### OVH outer-bootstrap installs `rsync`

The OVH `Debian 12 - Docker` image ships docker but not `rsync`, which the `mngr_vps_docker` build-context upload needs. Cloud-init-using backends (Vultr) inherit rsync from their base images; OVH has no cloud-init at all, so the gap surfaced as `bash: line 1: rsync: command not found` after every other outer-bootstrap step had already succeeded. New `install_required_outer_packages` helper in `mngr_ovh.bootstrap` runs as the final outer step before `VpsDockerProvider.create_host` takes over.
