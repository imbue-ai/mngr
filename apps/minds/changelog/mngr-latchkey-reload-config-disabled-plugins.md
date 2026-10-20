Marked the minds `test_prevent_if_elif_without_else` ratchet `@pytest.mark.flaky`. Its tree-wide AST parse can exceed the 10s pytest-timeout on a loaded offload sandbox, and it failed CI that way on an unrelated PR. The scan cost itself is tracked in MIND-182. Also removed the `# --- ... ---` section dividers from that ratchet file.

On a fresh install, the app's first workspace now gets a working latchkey gateway without a restart. The fix is in mngr's settings loader; see the libs/mngr and libs/mngr_latchkey changelog entries.
