## mngr

- Fixed `test_destroy_all_via_stdin` (e2e): added `@pytest.mark.timeout(120)` so the 2-agent create+destroy flow has time to run past pytest's default 10s timeout, and removed the incorrect `@pytest.mark.modal` mark (the test creates only local agents and never invokes the Modal CLI from the e2e subprocess, so the SDK-monkeypatch and PATH-wrapper guards never fired). Also added the corresponding `write_tutorial_block` call and stronger assertions on the destroy output (per-agent destroyed lines and total count).
