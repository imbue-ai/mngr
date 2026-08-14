# Reboot-resilience runbook: backfill sweep semantics updated

Updated `docs/reboot-resilience-rollout.md` Step 2 for the fixed backfill sweep: the installer now explicitly fires the workspace start (starting the path unit alone never re-runs a latched-active service), the sweep verifies the fired run actually succeeded before reporting a VM as `backfilled`, and containers that predate `minds_start_services_agent.sh` (minds-v0.3.1) degrade to a container+sshd start with a journal notice instead of retrigger-looping the unit.

Raised the e2e workspace-create readiness budget (`_CREATE_FORM_TIMEOUT_SECONDS`) from 600s to 900s: the budget covers a full docker build of the workspace image inside the CI sandbox, which legitimately takes ~8-10.5 minutes, so the old deadline had no headroom and failed the `build-minds-snapshot` job on roughly alternating runs (a healthy run measured 625s).
