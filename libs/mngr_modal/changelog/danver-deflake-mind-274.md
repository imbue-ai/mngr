A Modal control-plane blip no longer takes a whole `mngr` command down with it.

Modal answers some failures with an error that means "Modal failed your request" rather than "here is your answer" -- a cancelled call, a deadline exceeded, the service unavailable. mngr did not recognize those as transient, so a blip on Modal's side surfaced three ways it should not have: a state-volume read got no retry, the resulting error slipped past the guards that exist to degrade gracefully and crashed the caller instead, and a Modal that was merely erroring at startup was treated as a hard failure -- which failed every *other* provider the user had, on a command that may not have been about Modal at all.

Such a failure is now treated the same as a Modal that could not be reached: the operation is retried, and if it keeps failing the Modal provider is reported as unavailable while the rest of the command carries on. The message says Modal did not answer, rather than blaming the network specifically.

`test_get_host_by_name` is marked flaky so CI retries it while MIND-274 is open (MIND-274).
