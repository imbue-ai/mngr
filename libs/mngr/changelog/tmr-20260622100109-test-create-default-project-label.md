Fixed the `test_create_default_project_label` e2e tutorial test:

- Its verification step now scopes `mngr list` to the local provider (`--provider local`). The agent under test is a local command agent, so a bare `mngr list` would needlessly fan out to every registered backend and fail when an installed-but-unconfigured cloud backend (e.g. aws/azure/gcp without credentials) makes a full enumerate-all discovery abort.

- Removed the inaccurate `@pytest.mark.rsync` mark. The test creates a local agent, whose source transfer writes files directly rather than shelling out to rsync (rsync is only used for transfers to a remote host), so the resource guard correctly flagged the mark as never exercised.
