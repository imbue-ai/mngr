- Added an unhappy-path e2e release test (`test_create_modal_idle_mode_invalid`)
  for the `--idle-mode` create tutorial block, verifying that an unsupported idle
  mode is rejected by option parsing (exit code 2, listing the valid modes) and
  that no agent is created as a result.
