- Cut the **0.3.0** release of the minds desktop binary. Bumps
  `apps/minds/package.json` `version` to `0.3.0` and repoints
  `FALLBACK_BRANCH` in `apps/minds/imbue/minds/desktop_client/templates.py`
  from `v0.2.35` to the new FCT tag `v0.3.0` (at FCT commit `82a70518`).
  Every provider mode that clones FCT (lima / docker / vps_docker / vultr /
  ovh / imbue_cloud) lands on the same reviewed snapshot.

- The FCT v0.3.0 snapshot is the first release on the simpler-lima
  architecture (FCT PR #150 dropped docker-in-VM, runs agents directly in
  a lima VM as root) with the M5 lima-VZ SVE2 workaround baked in
  (FCT PR #151: `OPENSSL_armcap=0`). Verified end-to-end by launch-to-msg
  CI run 27288878538 with `skip_slack_flow=false` on
  `(mngr wz/minds_onboard, FCT main 82a705185)`.
