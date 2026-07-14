Temporarily enable the opt-in pytest `PHASE_TIMING` instrumentation in the `test-offload` recipe (passes `--env PHASE_TIMING=1` to offload) so a CI run emits per-batch phase-timing artifacts for the #2223 investigation. Revert this line once the data is collected.

Also adds a temporary `discovery-ab` workflow that times offload-style test discovery (the `all` + `flaky` --collect-only passes) serially vs in parallel on a GHA runner, to evaluate parallelizing them. Delete before merge.
