- New `minds-evals` CLI: `launch` an eval batch (one self-completing workspace per persona case), `list-batches` / `inspect` batch status straight from S3, and `restore` a case's per-turn snapshot into a local docker workspace.

- Eval runs are now fire-and-forget: each sandbox drives its own multi-turn conversation, snapshots `/mngr` to S3 with restic after each turn (via the create form's `api_key` backup provider), and uploads its transcript at the end. Results are read back from S3, so the launching machine does not need to stay on.
