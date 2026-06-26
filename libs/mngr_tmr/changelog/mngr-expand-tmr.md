TMR is now generic over any release test rather than specific to the mngr e2e
tutorial tests. Each test's docstring is the scope contract: the mapper agent
makes the test verify exactly what its docstring describes and treats the
docstring as read-only (the one exception being an outdated `Tutorial block:`
section, which it may correct along with `mega_tutorial.sh` as a `FIX_TUTORIAL`
change). The mapper and reducer prompts no longer re-derive scope from the
tutorial or assume e2e-only conventions.

The e2e-only `--mngr-e2e-run-name` flag is now injected only for e2e tests, so
TMR can run the non-e2e release tests (install/docker/cli and the per-provider
packages) without pytest erroring on an unrecognized argument.
