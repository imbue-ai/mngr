# Slice autostart backfill: actually fire and verify the workspace start

Two field-found fixes to `mngr imbue_cloud admin server backfill-autostart` (and the mirrored installer text), from the 2026-08-13 production pool-box recovery:

- The installer now explicitly fires the workspace start (`systemctl restart --no-block minds-autostart.service`). Starting the path unit alone never re-runs a service the old installer's boot-time oneshot left latched active (`RemainAfterExit=yes`), which made the sweep a silent no-op on exactly the wedge-recovered VMs it exists for.

- The sweep only reports a VM as `backfilled` after observing, via one in-VM wait loop, a service run that started after the install and ended in success -- previously it only checked that the path unit was active, which cannot distinguish a fresh success from the stale latched-active state.

- The in-container relaunch step now probes the known per-generation script locations itself and, for containers that predate `minds_start_services_agent.sh` entirely (minds-v0.3.1), starts the container plus its sshd and succeeds with a journal notice instead of failing. A permanently failing oneshot retriggered by the unthrottled path unit hot-looped at ~5 starts/second on such VMs. These successes carry a container-start-only note in the report's per-VM `detail`.

- The per-VM path extraction/substitution machinery is gone (the multi-path probe subsumes it); the pre-install run-stamp read now doubles as the per-VM reachability probe, so stopped or wedged VMs are still reported individually without aborting the box sweep.
