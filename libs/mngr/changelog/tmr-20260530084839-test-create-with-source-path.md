Removed the superfluous `@pytest.mark.modal` mark from the
`test_create_with_source_path` e2e release test. The test creates its agent on
the local provider (`mngr create --from <path>` with no host always targets the
local provider) and only runs `mngr list`/`mngr exec` afterwards, so it never
invokes the Modal CLI -- the only Modal usage the subprocess resource guard can
track. The mark therefore caused a guaranteed "marked with @pytest.mark.modal
but never invoked modal" failure. The test still carries `@pytest.mark.release`
(so it only runs in Modal-capable CI) plus `tmux`/`rsync`.
