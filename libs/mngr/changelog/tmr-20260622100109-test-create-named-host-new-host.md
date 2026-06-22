Fixed the `test_create_named_host_new_host` e2e release test: its verification
step now runs `mngr list --provider modal --addrs` instead of an unscoped
`mngr list --addrs`. The unscoped listing fanned out to other (unconfigured)
providers such as AWS, whose discovery errors made `mngr list` exit non-zero in
the isolated test environment even though the modal listing correctly showed the
named host. Scoping to the modal provider keeps the verification focused on the
behavior under test (the `name@host.provider --new-host` address syntax).
