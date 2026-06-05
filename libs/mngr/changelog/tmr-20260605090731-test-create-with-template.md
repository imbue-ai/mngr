Fixed the `test_create_with_template` e2e release test. The test creates a
local in-place agent (via a `transfer = "none"` create template), so it does
not exercise the Modal provider: removed the superfluous `@pytest.mark.modal`
(which the resource guard correctly flagged as never invoked) and scoped its
verification `mngr list` to `--provider local` so it no longer fans out to the
slow, network-bound Modal provider. Also strengthened the test to confirm the
agent's actual runtime working directory via `mngr exec my-task pwd`, not just
the `mngr list` metadata. No production behavior change.
