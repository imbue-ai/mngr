Fixed 21 release tests that failed on their resource marks rather than on anything they assert.

The resource guards fail a test that declares `@pytest.mark.rsync` or `@pytest.mark.modal` without exercising that resource, and the reverse: 17 tutorial tests declared `rsync` for local in-place agents that never shell out to it, two declared `modal` for listings that never reach the provider, and two `mngr_robinhood` streaming tests invoked rsync with no mark at all.

Two tests that the CI failure list also named are deliberately left alone. `test_ask_simple_query` and `test_create_codex_agent` reach rsync only through teardown log-preservation, which runs when they fail -- and both fail for real, unrelated reasons. Their marks are stale on the passing path and required on the failing one, so dropping them would only add guard noise to a failure that needs fixing on its own.

Dropped the `rsync` mark from the `mngr ask` release test. Its only rsync is the teardown that preserves logs when the test fails, so on the passing path the resource guard flagged the mark as never invoked. Removing it is safe in both directions: when the test does fail, preservation catches the guard block and warns rather than masking the real failure.
