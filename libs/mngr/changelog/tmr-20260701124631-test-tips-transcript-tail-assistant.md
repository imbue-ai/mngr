Added an explicit `@pytest.mark.timeout(120)` to the `mngr transcript --tail --role` tutorial e2e test so its `mngr create` setup step is no longer killed by the 10s default pytest timeout.
