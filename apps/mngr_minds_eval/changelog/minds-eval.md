- New `minds-evals` CLI: `launch` an eval batch (one self-completing workspace per persona case), `list-batches` / `inspect` batch status straight from S3, and `restore` a case's per-turn snapshot into a local docker workspace.

- Eval runs are now fire-and-forget: each sandbox drives its own multi-turn conversation, snapshots `/mngr` to S3 with restic after each turn (via the create form's `api_key` backup provider), and uploads its transcript at the end. Results are read back from S3, so the launching machine does not need to stay on.

- `restore` now recreates the workspace on Modal (workspaces are always Modal; the Docker box is only the mngr-branch isolation) and seeds the restored `/mngr` -- agent state, chat sessions, events -- into the new sandbox, so the restored workspace shows the conversation as it was rather than starting a fresh agent.

- The CLI is now a single host-native command: `launch` and `restore` build/boot the box from `--mngr-branch` if needed and re-run themselves inside it; `list-batches` and `inspect` read S3 directly and need no box. The `spin-up-minds-in-docker.sh` / `minds-evals.sh` shell wrappers are gone.

- `launch` now records the mngr branch it ran on in the batch config, and `restore` reads it back, so a restore rebuilds the same box the batch ran on instead of silently defaulting to a different mngr. `--mngr-branch` is still accepted as an override.
