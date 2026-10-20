Fix mngr host provisioning silently skipping all deploy files when the mngr host directory lives outside `$HOME` (e.g. a custom `MNGR_HOST_DIR`, or the modal acceptance-test layout). Deploy-file destinations are now derived from the mngr root-name convention (`~/.{root_name}/<path relative to the host dir>`) instead of the local file's position relative to `$HOME`, so `collect_provider_profile_files` no longer raises `ValueError` when the host dir is elsewhere -- which previously aborted the whole `collect_deploy_files` call and, because the error was downgraded to a warning, skipped provisioning of every deploy file.

Transient `*.lock` files (e.g. `.modal_ssh_key.lock`) in a provider's profile directory are now excluded from deployment.

Internal refactor (behavior-preserving): the `MNGR_ROOT_NAME` environment read (defaulting to `mngr`) is now centralized in a single `read_root_name()` helper in `config/host_dir.py`, replacing scattered inline `os.environ.get("MNGR_ROOT_NAME", "mngr")` copies across the config loader and CLI.

(GitHub issue #392)
