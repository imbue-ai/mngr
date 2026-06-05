Strengthened the `mngr --help` e2e test to assert that the help output lists the
other commands the tutorial advertises (`destroy`, `message`, `connect`, `clone`
in addition to `create` and `list`), and added an unhappy-path test verifying
that an unknown command fails and points the user back to `mngr --help`.
