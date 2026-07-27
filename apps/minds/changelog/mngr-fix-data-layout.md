Adaptations for the /home/user workspace data layout and env-converge:

- The in-place backup restore rewinds the backup root (`/home/user`; legacy `/mngr` workspaces keep their old target), and its restart-all step re-runs the `env-converge` one-shot so a restored environment record converges installed packages as well as files.

- minds-authored provider blocks (imbue_cloud accounts and byok aws/gcp/azure) now carry the layout knobs (`host_dir=/home/user/.mngr`, `volume_home_path=/home/user`, `host_log_dir=/var/log/mngr`).

- The release runbook gains step 0: cut (and warm) the apt snapshot mirror for a new timestamp via the connector admin routes before landing a `.mngr/apt-snapshot-timestamp` bump.

- Remote workspace hosts (docker, lima, vultr, aws, modal, gcp, azure launch modes) now receive `MNGR_HOST_DIR=/home/user/.mngr` (the new workspace layout's host_dir) instead of the legacy `/mngr`, and the snapshot-resume and litellm workspace tests exec against `/home/user/workspace` instead of `/code`.

- The workspace-recovery probe checks supervisord via `/home/user/workspace/supervisord.conf`, and backup restore locates the workspace subtree in a snapshot by its `workspace/` checkout (falling back to `code/` for legacy snapshots).

- The in-container recovery probe script and the in-workspace restore script's post-restore sanity check follow the new layout (`/home/user/workspace/supervisord.conf`; `workspace/` checkout marker with legacy `code/` fallback).
