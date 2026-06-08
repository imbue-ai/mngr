Fixed the `test_get_json_into_var` scripting e2e test. It now overrides the 10s
global pytest timeout (Modal provider discovery inside the `mngr list` subprocess
can exceed it) and no longer carries `@pytest.mark.modal`: `mngr list` discovers
Modal via the in-process Python SDK inside the subprocess, which the modal
resource guard (whose SDK monkeypatch lives only in the pytest process) cannot
observe, so the mark produced a spurious "never invoked modal" failure. The test
also strengthens its assertion to confirm the shell variable actually captured a
non-empty JSON document.
