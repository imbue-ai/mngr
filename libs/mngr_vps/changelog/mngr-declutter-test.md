Integration branch combining the user-data-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this project: the new optional `volume_home_path` config gives the unified host volume a `home/` subdirectory backing a configurable container home path (e.g. `/home/user`) with `host_dir` inside it, plain-text service logs honor the new `host_log_dir` config, and the clone-overlay comment follows the template declutter. Defaults unchanged.
