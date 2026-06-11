AWS agents now have a Modal-like idle-paused-but-resumable lifecycle: `mngr stop --stop-host` stops the EC2 **instance** itself (not just the inner Docker container), so a paused agent costs only EBS storage, and `mngr start` resumes it with the root EBS volume and all on-disk state intact. A stopped host still shows in `mngr list` (with its agents) and resolves by name for `mngr start`.

Under the hood:

- `AwsVpsClient` gained `stop_instance` (StopInstances, waits for the terminal `stopped` state), `start_instance` (StartInstances, waits for `running`, returns the fresh public IP), and `add_tags`/`remove_tags`.

- `AwsProvider` overrides `stop_host`/`start_host`: stop stops the container then the instance and records `stop_reason=STOPPED`; start locates the stopped instance by its `mngr-host-id` tag (it isn't SSH-reachable), starts it, and rebinds the host record + known_hosts to the instance's new public IP before restarting the container.

- Because a stopped instance has no public IP and drops out of SSH-based discovery, agent records are mirrored into EC2 tags (`mngr-agent-<id>`) as they are created/updated, and `AwsProvider` reconstructs stopped hosts and their agents from tags in discovery / `to_offline_host`. This keeps paused hosts visible and resumable by name. (Per-agent tags are capped by EC2's 50-tag / 256-char limits; an S3-backed store for many-agent hosts is a possible future follow-up.)

- New per-host IAM: `ec2:StopInstances`, `ec2:StartInstances`, `ec2:CreateTags`, `ec2:DeleteTags`.

Toward a self-stopping idle watcher (so an idle agent's instance can stop *itself*), `mngr aws prepare` now also provisions a `mngr-aws` IAM role, inline policy, and instance profile granting `ec2:StopInstances` scoped to `mngr-provider`-tagged instances (limited blast radius). The profile is not yet attached to instances (a later increment). `mngr aws cleanup` tears it down alongside the security group (still refusing while any mngr-managed instance exists). `prepare` additionally needs `iam:CreateRole`, `iam:PutRolePolicy`, `iam:CreateInstanceProfile`, `iam:AddRoleToInstanceProfile`; `cleanup` additionally needs `iam:RemoveRoleFromInstanceProfile`, `iam:DeleteInstanceProfile`, `iam:DeleteRolePolicy`, `iam:DeleteRole`.
