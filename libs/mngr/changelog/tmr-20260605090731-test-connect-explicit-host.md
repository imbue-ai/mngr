Fixed the `test_connect_explicit_host` e2e tutorial test, which was failing
because it carried `@pytest.mark.tmux`, `@pytest.mark.rsync`, and
`@pytest.mark.modal` resource-guard marks that the command never exercises.
`mngr conn my-task@my-host` fails during host resolution (the host does not
exist) and exits before attaching a tmux session, running rsync, or making any
real Modal call, so the resource guard rejected the superfluous marks. The test
now carries only `@pytest.mark.release`, matching the other unhappy-path connect
test. Also strengthened the assertion to verify the error clearly reports the
missing host rather than only checking the exit code.
