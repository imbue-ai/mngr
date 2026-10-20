`minds server prep` gained an optional `--extra-prep-script <file>`, forwarded verbatim to `mngr imbue_cloud admin server prep`.

Part of the log/telemetry aggregation rollout (`specs/minds-openobserve-telemetry.md`): `just prep-server` renders the observability collector install script and passes it through this flag whenever the tier's `secrets/minds/<tier>/observability` Vault entry carries a boxes ingest credential, so re-prepping a box is also how the OpenTelemetry Collector rolls out to the existing fleet.

The per-tier observability bring-up is documented in the operator runbook `apps/minds/docs/deploy/setup/observability.md` (resource creation, Vault population, provisioning, the manual Modal workspace step, the dev-tier validation gates, fleet collector rollout, and ongoing operations), with checklist pointers recorded in `apps/minds/docs/deploy/next_deploy.md` for the next deployment.
