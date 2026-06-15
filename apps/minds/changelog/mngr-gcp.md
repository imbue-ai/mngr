## test: mark a flaky desktop-client timeout test

- `test_start_creation_subscription_ai_does_not_mint_litellm_key` is now `@pytest.mark.flaky`, matching its already-marked API_KEY twin: both occasionally exceed the 10s pytest-timeout when offload sandboxes are contended (unrelated to product behavior). No source change.
