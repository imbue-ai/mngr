# mngr-minds-eval

Eval harness for Minds. Launch a batch of persona cases as **self-completing** workspaces: each
sandbox drives its own conversation, snapshots `/mngr` to S3 after every turn, and uploads its
transcript at the end. Results are read back from S3 -- the launching machine does not need to
stay on, and there is nothing to poll.

## Setup (once)

Put scoped AWS credentials at `~/.minds-eval/aws.env` (the box mounts it read-only):

```
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
MINDS_EVAL_BUCKET=minds-eval-backups-<account>
```

Use an IAM user scoped to that one bucket -- the keys are handed to every sandbox (the agent runs
arbitrary code), so they must not be able to touch anything else.

## Use

```
# the box (controller): any mngr branch; it is also what gets vendored into each case
scripts/spin-up-minds-in-docker.sh mngr-branch=minds-eval container-name=box1

# launch a batch (one self-completing workspace per case)
ANTHROPIC_API_KEY=sk-ant-... scripts/minds-evals.sh box1 launch \
    --name web1 --personas sample-personas.json --turns 4

# status, straight from S3 (works from anywhere, any time)
scripts/minds-evals.sh box1 list-batches
scripts/minds-evals.sh box1 inspect web1_20260713-101500

# bring a snapshot back up as a local docker workspace and click through what the agent built
scripts/minds-evals.sh box1 restore web1_20260713-101500 --case todo-app --message 2
```

## Turn logic (`--turns N`, from the case config)

| wait | the in-sandbox worker does |
|---|---|
| 1 | send the case's `first_prompt` |
| 2 .. N-1 | `restic backup /mngr --tag post_message_<k>`, then send `OKAY` |
| N | upload the full transcript, mark `finished`, exit |

Every wait writes `state.json` (`waits_done` / `num_turns` / `ongoing`|`finished`).

## S3 layout

```
<eval_name>_<utc-datetime>/          batch
  config.json                        cases + num_turns + restic password
  <eval_name>_<case_name>/
    state.json                       written by the worker each turn
    artifacts/full_transcript.jsonl  written by the worker on the final turn
    restic/                          the case's restic repo (tagged /mngr snapshots)
```

## Notes

- Snapshots are restic (deduped, encrypted, incremental), configured through the create form's
  `api_key` backup provider -- the idiomatic path. The worker stops the `host-backup` service in
  its own sandbox so restic only fires on the turns it chooses; `supervisord.conf` is unchanged, so
  non-eval workspaces are unaffected.
- The eval worker lives on the FCT `minds-eval-autosend` branch and no-ops unless
  `scripts/config.json` is present, so normal workspaces on that branch behave normally.
- Deps (`node_modules`, `.venv`) are excluded from snapshots; a restored workspace reinstalls them
  from the preserved lockfiles on first boot.
