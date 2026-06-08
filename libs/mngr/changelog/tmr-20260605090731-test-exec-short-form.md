Fixed the `test_exec_short_form` e2e tutorial test: removed the spurious
`@pytest.mark.modal` mark. The test creates a local `--type command` agent and
runs `mngr x my-task "git status"` on it, so it never provisions a Modal
environment and never invokes the `modal` CLI; the modal resource guard
therefore failed it with "marked with @pytest.mark.modal but never invoked
modal". Also strengthened the assertion to verify the short-form `mngr x`
actually ran git in the agent's work_dir (observing the `On branch` output)
rather than only checking the exit code.
