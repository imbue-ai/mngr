Removed `TagLimitExceededError`: it existed only to flag the EC2 50-tag ceiling for the AWS provider's offline tag mirror, which this PR replaces with the S3 state bucket.

Regenerated the `mngr aws` / `mngr azure` CLI doc pages to cover the state-bucket setup these commands now perform (the providers' state-bucket feature is described in the `mngr_aws` / `mngr_azure` changelogs).

Added shared operator-command output helpers in `mngr.cli.output_helpers`, used by the `mngr aws` / `mngr azure` / `mngr gcp` prepare/cleanup commands: `emit_operator_result(event_name, parts, output_format)` renders a sequence of `OperatorResultPart`s -- each pairing a structured-data fragment with its human line, built via `OperatorResultPart.shown(human, **data)` or `shown_if(present, human, **data)` -- as JSON / JSONL / human in one place, plus a `write_event_line` primitive for the shared `{"event": <type>, ...payload}` JSONL shape.

Test-only: raised the per-test timeout on the tmux lifecycle tests `test_start_restart_running_agent` / `test_start_restart_stopped_agent` from the default 10s to 30s (they run several sequential tmux create/stop/restart operations that can exceed 10s on a loaded CI runner).
