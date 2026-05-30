Fixed the `test_basic.py::test_create_and_destroy_agent` e2e release test, which
was missing the `@pytest.mark.timeout(60)` override carried by every other e2e
test that destroys an agent. `mngr destroy` performs host destruction plus a full
garbage-collection pass, which exceeds the global 10s pytest timeout; the test now
gets the same 60s budget as its peers.
