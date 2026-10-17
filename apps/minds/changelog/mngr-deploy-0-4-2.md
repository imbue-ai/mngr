Documented the minds-v0.4.2 staging and production deployments
(2026-08-25): new deploy-history entry
(`docs/deploy/history/minds-v0.4.2.md`) covering the staging deploy
`20260825T134133Z` and the production deploy `20260825T171504Z` plus its
`20260825T173747Z` ROLLOVER re-deploy that published the corrected policy
pages (migrations 028-030, RECREATE, both apps healthy in both tiers), the
post-connector redeploy of all 4 relays per tier (activating the ~10s
live-tunnel kill for suspend/unshare), the signup-IP-hardening `client_ip`
verification behind the custom domains in both tiers, the completed
post-deploy verifications (desktop fast path and the share/suspend
end-to-end pass on staging; Bugsink error reporting proven end to end on
production), and the minds-v0.4.2 staging pool bake.

Annotated `docs/deploy/next_deploy.md` checklist items (policy pages,
signup IP hardening, stop/start wedge fix, account suspension, Bugsink
bring-up) with their staging and production pass status.

The history entry also records the same-evening production pool fill (82
minds-v0.4.2 slices baked into the fleet's existing free slots instead of
buying new boxes: 27/27 US-EAST-VA, 55/56 US-WEST-OR with the one
phantom-slot refusal rolled back cleanly; fleet after 337/337 slots used)
and the desktop release-channel promotion (PR #602: stable, beta, and
alpha all serve the 0.4.2 build `260825un55i8ix7`).
