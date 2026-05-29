Added `**/tmr-report/` to the repo-root `.gitignore`. The TMR orchestrator
writes transient run state (e.g. `tmr-report/events.jsonl`) into this directory,
and the existing `**/tmr_*/` pattern did not match the hyphenated name.
