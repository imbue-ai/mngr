Fix scheduled deployments silently dropping the deployer's `config.toml` and top-level profile files when the mngr host directory lives outside `$HOME`. `get_files_for_deploy` now derives staged destinations from the mngr root-name convention (`~/.{root_name}/...`) rather than the local file's position under `$HOME`, so these files stage correctly regardless of where the host dir physically lives (previously the collection raised and all deploy files were skipped).

(GitHub issue #392)
