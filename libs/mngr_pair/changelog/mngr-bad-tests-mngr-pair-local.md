Strengthened the mngr-pair test suite. The `pair` CLI tests now assert on the
specific error messages and the exact set of choice values rather than on
near-tautological substrings, the `--source`/`--source-agent` conflict test now
actually triggers (and verifies) the conflict, and the structured (JSONL/JSON)
output of pair-start/stop events is checked. Internal-only changes; no
user-facing behavior changed.
