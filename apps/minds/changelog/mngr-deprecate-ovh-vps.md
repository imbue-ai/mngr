Deprecated baking new OVH classic VPS pool hosts. Imbue Cloud pool hosts are now baked exclusively as bare-metal slices.

`minds pool create` now defaults to `--backend slice`; `--backend ovh_vps` fails fast (before any Vault / credential resolution) with a deprecation error pointing at `--backend slice`. Existing OVH VPS pool hosts keep working and can still be listed (`minds pool list`) and destroyed (`minds pool destroy`, `minds env destroy`).

The host-pool docs (`host-pool-setup.md` and related) were rewritten around the bare-metal slice workflow; OVH is now described only as the current internal supplier of bare-metal boxes and in a "Legacy OVH VPS teardown" section. The per-tier OVH credentials are reframed as bare-metal box supplier credentials (still required, since they order the slice boxes and tear down legacy VPS hosts).
