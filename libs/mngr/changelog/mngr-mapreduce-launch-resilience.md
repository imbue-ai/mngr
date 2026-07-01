Acquiring a remote host's cooperative lock (part of every `mngr create`/`start` on a remote host) is now resilient to transient SSH failures. Previously, if the SSH connection to a host dropped ("Connection reset by peer" leaving a dead transport), opening the lock channel raised a raw paramiko `SSHException: SSH session not active` that leaked past callers.

The lock path now retries transient SSH errors after rebuilding the dropped connection (matching how remote shell/file operations already behave), and surfaces a failure that survives the retries as a structured `HostConnectionError` instead of a raw paramiko exception.

This keeps a single unreachable machine from crashing an operation that spans many hosts at once: the mapreduce/TMR orchestrator launches one agent per task and already isolates per-agent `MngrError`s, so one host with a flaky connection is now recorded as a single failed launch rather than aborting the entire run.
