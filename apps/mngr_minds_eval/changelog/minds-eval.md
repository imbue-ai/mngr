- New `minds-evals` CLI: `launch` an eval batch (one self-completing workspace per persona case), `list-batches` / `inspect` batch status straight from S3, and `restore` a case's per-turn snapshot into a local docker workspace.

- Eval runs are now fire-and-forget: each sandbox drives its own multi-turn conversation, snapshots `/mngr` to S3 with restic after each turn (via the create form's `api_key` backup provider), and uploads its transcript at the end. Results are read back from S3, so the launching machine does not need to stay on.

- `restore` now recreates the workspace on Modal (workspaces are always Modal; the Docker box is only the mngr-branch isolation) and seeds the restored `/mngr` -- agent state, chat sessions, events -- into the new sandbox, so the restored workspace shows the conversation as it was rather than starting a fresh agent.

- The CLI is now a single host-native command: `launch` and `restore` build/boot the box from `--mngr-branch` if needed and re-run themselves inside it; `list-batches` and `inspect` read S3 directly and need no box. The `spin-up-minds-in-docker.sh` / `minds-evals.sh` shell wrappers are gone.

- `launch` now records the mngr branch it ran on in the batch config, and `restore` reads it back, so a restore rebuilds the same box the batch ran on instead of silently defaulting to a different mngr. `--mngr-branch` is still accepted as an override.

- The box is now keyed to the mngr branch tip: its image is tagged `minds-box:<branch>-<sha>` and the running box is stamped with the SHA it was built from. `ensure` reuses a running box only when it matches the branch's current tip, and rebuilds when the branch has moved -- so launch/restore never silently run a stale mngr. Reuse stays instant; restore speed is unaffected (it is dominated by the new Modal workspace, not the box).

- Fixed backup wiring: minds assigns each workspace its own restic password and rejects a caller-supplied one, so `launch` no longer sends `RESTIC_PASSWORD` (only the repo URL + AWS creds). The in-sandbox worker uploads the generated password to the case's S3 prefix, and `restore` reads it from there -- so a repo stays decryptable after the box and sandbox are gone.

- Added `minds-evals box --mngr-branch X` as a first-class utility to build/boot a Minds box for any mngr branch (previously box spin-up only happened as a side effect of launch/restore).

- Added `minds-evals box --mngr-branch X` as a first-class utility to build/boot a Minds box for any mngr branch (previously box spin-up only happened as a side effect of launch/restore).

- Unified the CLI: `workspace` (create one ad-hoc workspace in a box) is now a `minds-evals` subcommand like the rest, and the last shell wrapper is gone. Everything is one Python CLI (`minds-evals launch|box|workspace|list-batches|inspect|restore`); the only remaining shell file is the Docker container entrypoint, which must be bash.

- `launch` now takes `--fct-branch` / `--fct-repo` (the workspace-template each case is cloned from), instead of hardcoding the branch. Recorded in the batch config. Default remains the branch that carries the eval worker.
