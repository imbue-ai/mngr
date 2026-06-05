Strengthened the `mngr config path --scope user` e2e tutorial test to verify the
reported path is actually the user-scope config file (by writing a value at user
scope and confirming it lands in that exact file), and added an unhappy-path test
that asserts an unsupported `--scope` value is rejected.
