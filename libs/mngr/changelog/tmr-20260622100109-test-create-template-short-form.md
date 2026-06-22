Added an explicit `@pytest.mark.timeout(120)` to the `test_create_template_short_form` e2e tutorial test so it no longer fails under the global 10s pytest timeout while `mngr create` starts an agent.
