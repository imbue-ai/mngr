Slice-fleet-gen2 phase 2 (specs/slice-fleet-gen2):

- `bare_metal_servers.status` gains the `draining` value: the fleet-turnover exit (`ready` -> `draining` -> repave) driven by the new `minds-admin server drain`; the provisioning step-forward helpers deliberately do not cover it.

- The `LeaseResult` and `WorkspaceInfo` wire models gain an additive `box_generation` field (default 1 against an older connector), and `providers/rebuild.py` now selects the slice VM client through the generation-dispatch factory on that field instead of hardcoding the lima client.
