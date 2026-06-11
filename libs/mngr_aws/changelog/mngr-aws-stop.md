AWS agents now have a Modal-like idle-paused-but-resumable lifecycle: `mngr stop --stop-host` stops the EC2 **instance** itself (not just the inner Docker container), so a paused agent costs only EBS storage, and `mngr start` resumes it with the root EBS volume and all on-disk state intact.

Under the hood:

- `AwsVpsClient` gained `stop_instance` (StopInstances, waits for the terminal `stopped` state) and `start_instance` (StartInstances, waits for `running`, returns the fresh public IP).

- `AwsProvider` overrides `stop_host`/`start_host`: stop stops the container then the instance and records `stop_reason=STOPPED` (so a paused host lists as STOPPED, not CRASHED); start locates the stopped instance by its `mngr-host-id` tag (it isn't SSH-reachable), starts it, and rebinds the host record + known_hosts to the instance's new public IP before restarting the container.

- New IAM permissions on the per-host path: `ec2:StopInstances`, `ec2:StartInstances`.
