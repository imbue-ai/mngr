## dev

- TMR workflows (`tmr.yml`, `tmr-reintegrate.yml`) now re-assert `mngr tmr`'s exit code via `exit "${PIPESTATUS[0]}"` after the `| tee tmr-report/events.jsonl` pipeline. The implicit `pipefail` propagation was observed to not catch the left-side failure in this step, letting a failed run be reported as successful.
