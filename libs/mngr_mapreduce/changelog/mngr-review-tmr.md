Added a `--reducer-env` option for passing environment variables to the reducer agent only, never to the mappers.

Map-reduce runs launch one mapper per task but a single reducer, and the reducer is the natural place to do privileged work at the end of a run (for TMR, opening the run's pull request). Handing a push-capable token to every mapper to achieve that would spread a write credential across the untrusted bulk of the run, so `--reducer-env KEY=VALUE` keeps it to the one agent that needs it.

Values are merged over `--env`, so the reducer sees the shared environment plus its own additions, and a reducer-only value wins on a key collision.
