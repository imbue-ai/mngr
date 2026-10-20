The `mngr` wheel no longer ships two test-only helper modules.

`imbue/mngr/agents/agent_release_testing.py` and `imbue/mngr/providers/provider_release_testing.py` hold the shared arcs and assertions that the per-plugin agent and provider release tests drive. Nothing `mngr` runs imports them: every importer is a release test, except one `testing.py` that is itself excluded from its own wheel. Both were being packaged anyway, because the wheel's `**/testing.py` exclude rule matches only that exact filename and not a longer name ending in `_testing.py`.

They are now named in full in the `exclude` list, so a `pip install imbue-mngr` gets 97,842 fewer bytes of test code (26,919 fewer compressed) and no test-only import paths. The modules stay where they are in the checkout, so the release tests that import them are unaffected.
