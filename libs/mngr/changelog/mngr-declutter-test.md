Integration branch combining the user-data-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this project: the docker provider gains the additive `volume_mount_path` and `host_log_dir` knobs, host records persist their `host_dir` (used by discovery, start, and snapshot restore), and the regenerated `imbue_cloud` CLI reference follows the template declutter (`system/vendor/mngr/`).
