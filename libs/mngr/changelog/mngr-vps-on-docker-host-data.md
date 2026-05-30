`build_check_and_install_packages_command` (in `providers/ssh_host_setup.py`) now
`mkdir -p` the symlink target before creating the `host_dir` symlink. The local
`docker` provider already creates that subdirectory eagerly so it's a safe no-op
there; the new docker_vps unified-volume layout relies on the in-script mkdir to
seed `<volume>/host_dir` before pointing `/mngr` at it.
