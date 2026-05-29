Removed the superfluous `@pytest.mark.modal` mark from the e2e tutorial test
`test_create_with_project_label`. The test only creates a local command agent
and runs `mngr list`; since no Modal environment exists, the Modal backend
disables itself (`ProviderEmptyError`) and no Modal network call is made, so the
resource guard correctly rejected the mark.

Added a companion e2e test `test_create_default_project_label` covering the
default-project-label path of the same tutorial block: when `--project` is
omitted, the agent's `project` label is derived from the source's folder name
(the git repo has no remote).
