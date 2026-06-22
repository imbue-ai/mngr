- The `test_create_modal_build_args` e2e tutorial test now provisions a
  lightweight `command`-type agent (`-- sleep ...`) instead of a `claude` agent.
  The build args under test (image, cpu, memory) are independent of the agent
  type, so this avoids a needless Claude Code install plus the spurious "No API
  credentials detected" warning, and aligns the test with the sibling custom-build
  tests.
