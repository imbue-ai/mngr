AWS agents are moving toward a Modal-like idle-paused-but-resumable lifecycle: idle stops the EC2 instance (preserving the EBS volume and all state) instead of just the Docker container, and `mngr start` resumes it.

This entry covers the first increment: `AwsVpsClient` gained `stop_instance` (StopInstances, waits for `stopped`) and `start_instance` (StartInstances, waits for `running`, returns the fresh public IP). The provider-level `mngr stop`/`mngr start` wiring builds on these.
