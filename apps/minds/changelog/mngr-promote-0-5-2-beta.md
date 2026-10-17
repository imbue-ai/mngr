Promoted minds 0.5.2 to the beta channel (build `260909wnd3gb4z1`, `minds-v0.5.2`), at 100%. Alpha is already on it; stable stays on 0.5.0 build `260902shwco3ynx`.

Beta requires the tier services to be deployed, unlike alpha. That landed with 0.5.2 (`deploy_id 20260909T225715Z`), which moved the web-create pin from `minds-v0.4.3` to `minds-v0.5.2`, so browser and desktop creates target the same tag. The production pool holds 20 slices at the tag, so beta clients get the fast path rather than a rebuild.
