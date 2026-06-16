# GCP & Azure stop/start lifecycle (idle-pause + resume)

Status: **In progress.** Brings the AWS stop/start lifecycle
(`specs/aws-ec2-stop-start-lifecycle/spec.md`) to the GCP and Azure providers so
`mngr stop` halts live-instance compute billing (the boot/OS disk and all state
persist; storage still bills) and `mngr start` resumes the session faithfully with
all files intact. Branch: `mngr/stop-gcp-azure`, on a synthetic base of
`mngr/azure` + `mngr/aws-stop` + `mngr/separate-snapshots` (`mngr/stop-gcp-azure-base`).

## Goal

Parity with AWS: when a GCP/Azure agent is stopped (manually via `mngr stop` or by
the idle watcher), the underlying VM's compute billing ends while its disk and all
on-disk state survive, and `mngr start` resumes it. A stopped VM must still appear
in `mngr list` and resolve by name (offline discovery), exactly as on AWS.

## Reference: the AWS shape

AWS (`mngr_aws/backend.py`, `client.py`) established the pattern this spec ports:

- **Client lifecycle methods** beyond the shared `VpsClientInterface`:
  `stop_instance` / `start_instance` (start returns the possibly-new public IP),
  `_instance_state_name` + `_wait_for_instance_state` for polling, and tag
  read/write helpers (`add_tags` / `remove_tags`).
- **Provider overrides**: `stop_host` (super().stop_host(create_snapshot=False,
  stop_reason=STOPPED) then stop the instance), `start_host` (find the instance by
  id-tag while stopped → start → rebind known_hosts to the new IP → read+rewrite the
  on-volume record's `vps_ip` → super().start_host()), `_find_instance_for_host`
  (id-tag lookup that works while the box is unreachable).
- **Idle self-stop**: an in-container watcher touches a sentinel on the shared
  volume; a host-side systemd `.path` unit fires a oneshot `.service` that powers
  the box off. On AWS, `InstanceInitiatedShutdownBehavior=stop` turns the poweroff
  into an instance stop with no IAM.
- **Offline discovery via tags**: host + per-agent metadata is mirrored into
  instance tags so a stopped (SSH-unreachable) box still lists in `mngr list`,
  resolves by name, and resumes. Implemented by overriding `persist_agent_data`,
  `remove_persisted_agent_data`, `list_persisted_agent_data_for_host`,
  `discover_hosts_and_agents`, `get_host`, `to_offline_host`, `list_snapshots`.

## GCP design

GCE maps onto the AWS shape almost directly, with two differences (idle self-stop
is *simpler*; offline discovery needs a different metadata channel).

1. **Native stop/start.** `GcpVpsClient.stop_instance` → `instances.stop`, poll to
   `TERMINATED`; `start_instance` → `instances.start`, poll to `RUNNING`, return the
   fresh external IP. GCE `stop` preserves the (auto-delete-on-*instance*-delete)
   boot disk; a stopped instance keeps its disk and stops compute billing. The
   ephemeral external IP is released on stop and a new one is assigned on start, so
   `start_host` rebinds known_hosts to the new IP exactly like AWS. (`TERMINATED` is
   GCE's confusing name for a *stopped* — not deleted — instance.)

2. **Idle self-stop needs no API call and no stop-vs-terminate flag.** A guest-OS
   `shutdown -P now` lands a GCE instance in `TERMINATED` (stopped, disk preserved,
   no compute billing) by default — there is no GCE analog to AWS's
   `InstanceInitiatedShutdownBehavior`, and none is needed. So GCP reuses the AWS
   sentinel + host-side systemd watcher verbatim (the `.service` just runs
   `shutdown -P now`), and the instance stops. The existing `auto_shutdown_seconds`
   time-cap (`max_run_duration` + `instance_termination_action=DELETE`) is an
   orthogonal *delete* safety net (mainly for tests) and is left as-is; idle →
   stop, time-cap → delete, no conflict. No new config field is required for GCP.

3. **Offline discovery uses instance metadata, not labels.** GCE *labels* are
   restricted to `[a-z0-9_-]`, <=63 chars, and are lowercased — they cannot hold an
   agent's raw name/type or a labels-JSON blob, and would mangle a mixed-case host
   name. So GCP mirrors the host name and per-agent records into instance
   **metadata** (permissive, large values), keeping *labels* only as the discovery
   filter keys (`mngr-provider`, `mngr-host-id`, `mngr-created-at`, already present).
   - Host name → metadata key `mngr-host-name` (preserves case).
   - Per-agent records → metadata keys `mngr-agent-<id>-<field>` for
     `field in {name, type, labels}` (labels as compact JSON), mirroring AWS's tag
     layout but in the metadata namespace.
   - GCE `setMetadata` is a whole-object read-modify-write guarded by a
     `fingerprint`; the upsert/delete helpers read the live instance, merge the
     changed items, and `setMetadata` with the current fingerprint, retrying once on
     a `412`/fingerprint conflict (concurrent agent writes are rare but possible).
   - `list_instances` is extended to also surface each instance's metadata items so
     the offline-discovery reconstruction can read them without a second GET.

## Azure design

Azure is the divergent one: an OS-level shutdown does **not** stop compute billing
(the VM is left "Stopped (not deallocated)"); only an ARM `deallocate` call does.

1. **Deallocate/start.** `AzureVpsClient.deallocate_instance` →
   `virtual_machines.begin_deallocate`, poll to `PowerState/deallocated`;
   `start_instance` → `begin_start`, poll to `PowerState/running`, return the public
   IP. Azure already allocates a **static** public IP (`client.py`), so the IP is
   *preserved* across deallocate/start — `start_host` does **not** need the AWS-style
   known_hosts IP rebind (the IP and SSH host keys both survive on the OS disk). The
   record's `vps_ip` is unchanged; `start_host` only clears `stop_reason` and
   relaunches the container/watcher via `super().start_host()`.

2. **Manual `mngr stop` deallocates** via the API from the mngr process (this always
   works regardless of operator privilege).

3. **Idle self-deallocate via system-assigned managed identity (with graceful
   fallback).** Because an OS poweroff can't stop Azure billing, the in-VM idle path
   must call the ARM `deallocate` API. Design:
   - The VM is created with a **system-assigned managed identity**.
   - A **least-privilege custom role** (`Microsoft.Compute/virtualMachines/deallocate/action`
     + `read`) is created once by `mngr azure prepare` (subscription scope,
     best-effort), and a **per-VM role assignment** of that role to the VM's identity
     is made at create time (resource scope = the single VM).
   - The in-VM idle watcher (sentinel → systemd) runs a oneshot that fetches an IMDS
     token (`169.254.169.254`, no az CLI) and POSTs ARM
     `.../virtualMachines/<vm>/deallocate` (202 returns before the guest dies). VM /
     RG / subscription identifiers come from IMDS at runtime.
   - **Graceful fallback (auto-detect):** creating the custom role definition needs
     `roleDefinitions/write` and the role assignment needs `roleAssignments/write`
     (Owner / User Access Admin). If the operator lacks these, the authorization
     error is caught, a single clear WARNING is logged ("idle self-deallocate
     disabled; only `mngr stop`/`start` halt billing -- an in-VM OS shutdown does
     not, on Azure"), and create proceeds. This preserves the low-privilege flow
     that works today while giving privileged operators true cost parity. See
     `questions.local.md` Q2/Q2b for the decision record.

4. **Offline discovery via Azure tags.** Azure resource tags are permissive (256-char
   values, ~50 tags), so Azure mirrors host + per-agent metadata into VM tags exactly
   like AWS's EC2 tags (same `mngr-agent-<id>-<field>` layout), reusing the same
   reconstruction logic.

## Out of scope (follow-ups)

- GC stop-instead-of-destroy + age-gated terminate (AWS Phase 3) — not done for any
  provider yet.
- An external agent store for hosts with more agents than the tag/metadata mirror
  can hold (AWS Phase 5).
- Azure DevTest-style daily auto-shutdown as an additional backstop (time-of-day
  only; cannot express elapsed-from-boot, so not a substitute for the idle watcher).
