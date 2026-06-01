The minds app now consumes the `mngr imbue_cloud bucket` capability: when a
workspace is created with the `imbue_cloud` backup provider, minds calls
`mngr imbue_cloud bucket create` / `bucket keys create` to provision a
per-workspace R2 bucket (named after the host id) and a scoped readwrite key,
then points the workspace's restic backups at it.

(This integration PR adds no code in this project; it wires the existing
bucket commands into the minds workspace-creation flow. The bucket commands
themselves are covered by the `mngr-cloud-bucket` changelog entry.)
