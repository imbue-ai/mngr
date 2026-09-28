Bring-your-own-key GCP and Azure workspaces now run the agent container under gVisor (runsc), exactly as AWS workspaces do: the account writer sets `install_gvisor_runtime` and `docker_runtime = "runsc"` on every BYOK cloud account block. Prototyped on both clouds first (Debian 13 VMs, gVisor's systrap platform); both passed the gVisor, tmpfs, socket hard-link, and stop/start checks. Existing account blocks keep their settings; re-add the account to pick up the new shape.

- The AWS account block no longer carries `default_start_args = ["--tmpfs", "/run"]`: mngr_vps now mounts `/run` and `/tmp` on tmpfs itself whenever the runtime is runsc, on every cloud.

- Every cloud workspace container is now memory-capped at the VM's RAM minus 1 GiB and its VM has hardened sshd and bounded Docker logs, build cache, and journal (the gen-2 slice lessons, issue #976; see `libs/mngr_vps/changelog/mngr-cloud-provider-updates.md`).

- New release tests `apps/minds/test_gcp_workspace_release.py` and `apps/minds/test_azure_workspace_release.py` mirror the AWS one; all three now also assert the tmpfs mounts and the memory cap. Their shared driving and assertions live in `imbue.minds.testing`. minds now depends on the `imbue-mngr-gcp` and `imbue-mngr-azure` plugins, the two BYOK backends it writes blocks for that were not yet listed.
