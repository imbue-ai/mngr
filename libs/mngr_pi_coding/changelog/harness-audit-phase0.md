- Reformatted `resources/mngr_pi_lifecycle_test.py` with ruff (no behavior change).

- Corrected the stale per-agent `npm install` cost comment in `plugin.py` (observed 45-55s, not ~1s; it delays the readiness sentinel by that long) and two stale `ON_CHANGE` model-bar comments in `resources/mngr_pi_lifecycle.ts` (all harnesses reconcile via `EAGER_THEN_RECONCILE`).
