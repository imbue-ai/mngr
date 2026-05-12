Add `mngr usage wait`: block until a usage snapshot matches a CEL
predicate, then exit 0. Useful for composing with `mngr message` / `mngr
create` to launch new work once budget conditions are met (e.g. "75% of
the 5h window has elapsed and at most 50% of the limit has been used"):

```
mngr usage wait --until 'five_hour.elapsed_percentage > 75 && five_hour.used_percentage < 50' \
  && mngr message my-agent "ok, kick off the next batch"
```

The CEL context per source matches `mngr usage --format json`'s
`sources[i]`. Exit codes mirror `mngr wait` (0 matched, 1 error, 2
timeout). `--source NAME` restricts which writer sources count for
matching; default poll interval is 30s.

The Claude writer now also emits `window_seconds` per fixed-duration
window (`five_hour=18000`, `seven_day=604800`), enabling the reader to
derive `elapsed_seconds` / `elapsed_percentage` per window. These new
fields are surfaced in `mngr usage --format json` output (alongside the
existing `seconds_until_reset`) and are available to `mngr usage wait`
CEL predicates. Variable-duration windows (Claude's overage) intentionally
omit `window_seconds`, so the derived fields are `null` there.

Internal: shared exit-code constants moved from `mngr_wait.primitives`
to `mngr.cli.exit_codes`, callable from both `mngr_wait` and
`mngr_usage`.
