Fixed the `test_create_with_transfer_none` e2e release test, which was failing
because it carried a superfluous `@pytest.mark.modal`. An in-place
(`--transfer=none`) agent runs on the local host and never provisions a Modal
environment, so the modal resource guard rejected the mark. Removed the mark and
made the work-dir assertion robust to symlinks (via `os.path.realpath`) so it
holds on macOS, and added a direct check that the agent's `initial_branch` is
null.
