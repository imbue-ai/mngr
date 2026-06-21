- The Electron e2e workspace runner (`create_workspace_via_electron`) now accepts
  `launch_mode`, `region`, and `account_label`, so it can drive workspace creation
  in compute modes other than local Docker (e.g. Lima, AWS). Used to live-test the
  container/VM restart-recovery behavior.
