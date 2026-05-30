Fixed the `test_create_named_host_new_host` Modal e2e release test, which was
failing because the isolated e2e profile sets no default agent type (so bare
`mngr create` now errors after the "reject unknown agent types" change). The
test now passes `--type claude` explicitly, matching the tutorial's documented
premise that claude is the default agent type. Also strengthened the test to
verify the host was actually named `my-modal-box` via the address syntax (and
the agent named `my-task` with provider `modal`) using `mngr list --format
json`, rather than only checking the exit code.
