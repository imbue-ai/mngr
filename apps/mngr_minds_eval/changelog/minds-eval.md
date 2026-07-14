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

- Login URL reliability: `print_view_urls` never prints a silent nothing for the workspace-login line (if it can't find the URL it tells you how to get it), and a new `minds-evals login --box <name>` (or `--mngr-branch`) fetches the dashboard + one-time login URL on demand, so you no longer have to dig through docker logs.

- Idempotent launch: if a workspace with a case's host name already exists (a re-run with the same --name, or an interrupted prior run), it is destroyed before re-creating, instead of failing with "Host name already exists". mngr registers the name in the Modal environment, so it survived box restarts before.

- Fixed the workspace-login URL not being detected on mngr versions where the forward proxy runs with --use-http2 (it emits an https:// login URL); the matcher now accepts http or https.

- Fixed the eval worker crashing with NoCredentialsError: creds are now slotted into each clone's config.json (backup_provider=configure_later; the worker drives restic itself), instead of relying on minds' api_key backup provider, which does not land a restic.env inside a Modal sandbox.

- Removed the `login` subcommand (the login URL is printed by box/launch/restore). `list-batches` now prints the full batch folder name to pass to inspect/restore.

- Added `minds-evals clean-modal-workspaces --mngr-branch X` to destroy all workspaces in a box (clean slate) -- each destroy tears down the Modal sandbox and removes its host record from the Modal environment.

- The Modal environment (and box) for an eval run are now keyed on the eval NAME, not the mngr branch: `launch --name X` puts that run's sandboxes in `minds-<env>-X` under box `minds-box-X` (built from `--mngr-branch`), so a run's workspaces are findable and separable. `restore` reuses the batch's own name/branch; `clean-modal-workspaces --name X` targets that run's env. The general `box`/`workspace` utilities still key on the branch.

- De-duplicated workspace creation: launch, workspace, and restore now share a single `minds_client.create_and_wait` (POST + poll) instead of three near-identical copies.

- Removed the user-facing `self-check` subcommand; its asserts are now a real unit test (`main_test.py`). Clarified that `clean-modal-workspaces` is identified by `--name` alone (the Modal env); `--mngr-branch` is only a fallback to build a box when the eval's box isn't already running.

- launch now creates each case via workspace.create_workspace instead of its own build_create_payload, so the create payload shape is defined in exactly one place. create_workspace returns the agent id and raises minds_client.CreateError (callers decide abort vs continue).

- Box + Modal env keying reworked: container is minds-box-<branch>-<sha> (encodes the exact mngr, so reuse is idempotent and never stale; the Dockerfile now checks out that exact SHA), and the Modal env is the branch alone (stable for clean/list). restore rebuilds at the batch's recorded mngr_sha -> the exact mngr.

- launch now takes a single `--config eval_config.json` ({name, turns, mngr_branch, fct_branch?, fct_repo?, personas}) stored verbatim in S3 as the batch config. `workspace` renamed to `make-modal-workspace` (Modal is fixed; the `compute` knob is gone). The per-clone metadata file is renamed config.json -> test_case_metadata.json. sample-personas.json -> sample-eval-config.json.

- Removed `restore` (unverified). Modal env is now one shared `minds-staging-evaluator` for all eval workspaces (boxes stay versioned as minds-box-<branch>-<sha>); `clean-modal-workspaces` takes no args and wipes that env via any running box. launch creates workspaces in parallel (prime-then-fan only when the env is new) with a compact in-place live status table. Dropped dead code: restore.py, get_text, login_url, the login subcommand, pinned_ref, print_view_urls wait flag, the compute knob.

- Added `minds-evals evaluate <batch>` -- the evaluator half of the harness. It reads a finished batch from S3 (no box, no Modal), checks every case is finished, nukes any prior `case_eval_results.json` / `batch_eval_results.json`, then scores each case in parallel and writes results back to S3: `avg_word_count` (average words per agent turn, from the transcript) plus three LLM-graded 1-10 scores from one Anthropic call per case (`conciseness_score`, `nontechnical_language_score`, `proactive_score`). Per-case results land in `<case>/case_eval_results.json`, the batch average in `<batch>/batch_eval_results.json`; the command prints a table (rows = cases, columns = the keys) plus a batch-average row. Requires `ANTHROPIC_API_KEY`. New evaluations are added by appending a function to `EVALUATIONS` in `evaluate.py`.

- Config schema: each case now specifies a `prompts` array (one entry per turn) instead of a single `first_prompt`, and the batch-level `turns` field is gone -- a case's turn count is `len(prompts)`, so different cases can run different numbers of turns. Each entry is either a literal string (sent to the agent verbatim) or the sentinel `DECIDE_FROM_PERSONA`, which the in-sandbox worker resolves by role-playing the client via the Anthropic API (transcript-so-far + persona -> a short casual reply). The launch key is slotted into each case's `test_case_metadata.json` as `anthropic_api_key` for those calls. The two sample configs (`sample-eval-config.json`, `realasks-eval-config.json`) are combined into one `eval-config.json` (9 cases, 4-5 turns each: opener + a couple hard-coded "Sounds good." + `DECIDE_FROM_PERSONA` for the rest).
