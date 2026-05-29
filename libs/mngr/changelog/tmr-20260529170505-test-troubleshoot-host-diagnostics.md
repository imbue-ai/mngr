Fixed the TROUBLESHOOTING tutorial's host-diagnostics block: `mngr exec` takes
the command as a single quoted final argument (`mngr exec my-task "ps aux"`),
not via a `--` separator. The previous `mngr exec my-task -- ps aux` form caused
`mngr` to treat the command words (`cat`, `ps`, ...) as additional agent names
and fail with "Agent not found". The corresponding e2e release test
(`test_troubleshoot_host_diagnostics`) was updated to use the quoted form, to
drop a superfluous `modal` marker (the test only exercises a local command
agent), and to assert on the actual host-diagnostic output.
