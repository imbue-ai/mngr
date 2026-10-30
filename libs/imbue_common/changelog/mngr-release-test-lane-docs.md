The pytest tier docstring described release tests as running via "the Release Tests workflow and
TMR". That workflow is named `mngr Release Tests`, and it is only one of two lanes: it excludes the
`apps/minds` tree by path, and minds' release tests run from the minds jobs in `ci.yml` behind that
workflow's `run_minds_release_tests` dispatch input. The docstring now names both.
