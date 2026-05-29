Strengthened the `mngr create --help` e2e test to also assert the command
description is shown, and added an unhappy-path companion test that verifies an
undocumented option is rejected with a usage error (exit code 2) pointing back
to the help output.
