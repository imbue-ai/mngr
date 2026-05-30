The connector's R2 bucket + bucket-key endpoints are now exercised end-to-end
by the minds workspace-creation flow (via `mngr imbue_cloud bucket ...`) to
provision per-workspace restic backup buckets.

(This integration PR adds no code in this project; it wires the existing
bucket endpoints into minds. The endpoints themselves are covered by the
`mngr-cloud-bucket` changelog entry.)
