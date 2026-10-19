The desktop app now logs every GPU-process death with its reason, exit code, how many GPU deaths the run has seen, whether a shutdown or a SIGTERM/SIGINT had already happened, the app's uptime, and the GPU feature status. The termination-signal log lines now include the uptime too. This is diagnostics for the app aborting with "GPU process isn't usable. Goodbye.".

Sentry events from an app launched by the e2e workspace harness are tagged `launched_by=e2e_harness`, so they can be told apart from a developer's own install.

The e2e workspace harness forwards the app's GPU-death, termination-signal, and signal-exit output lines at info level instead of debug, and logs how the app's teardown went: whether it had already exited, and its exit code after SIGTERM or SIGKILL.
