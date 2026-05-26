## mngr

- e2e test `test_create_and_destroy_agent` in `test_basic.py`: added `@pytest.mark.timeout(60)` and removed the spurious `@pytest.mark.modal` mark. The default 10s timeout was killing `mngr destroy` mid-run, and the modal mark fired the "never invoked modal" guard now that `mngr destroy`/`mngr list` no longer auto-bootstrap Modal environments (since the `is_for_host_creation` change).
