Integration branch for the imbue_cloud slice-fleet generation 2 program (raw qemu slices with routed-tap networking on Debian 13 boxes, replacing lima), merged together with `main` and the three pre-cutover fix PRs: imbue-ai/mngr-internal#857 (SSH certificates from Vault, #850), #855 (DHCP placement, #849) and #856 (the upstream artifact mirror, #851). The per-change details live in this directory's constituent entries, listed below.

`modal deploy` retries on Modal's app-lock error for up to five minutes of elapsed time and exposes `DEPLOY_MAX_DURATION_SECONDS`.

Constituent entries: `mngr-mirror-upstream-artifacts.md`
