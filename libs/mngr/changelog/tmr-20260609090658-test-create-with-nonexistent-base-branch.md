Fixed a duplicated `type = "claude"` key in the e2e test fixture's generated
`settings.local.toml`, which made `tomllib` reject the file and caused every
`mngr` command in e2e tests to fail with a config parse error instead of
exercising the real code path. Also strengthened the nonexistent-base-branch
e2e test to assert the failure is actually about the missing base branch, so it
can no longer pass for an unrelated reason.
