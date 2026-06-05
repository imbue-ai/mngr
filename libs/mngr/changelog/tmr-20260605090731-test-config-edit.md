Strengthened the `mngr config edit` e2e tutorial test: it now drives the command
with a fake editor and verifies the editor is invoked on the freshly-created
config file (rather than only checking the exit code). Added a companion test
covering the unhappy path where the editor exits non-zero and the command
propagates the failure.
