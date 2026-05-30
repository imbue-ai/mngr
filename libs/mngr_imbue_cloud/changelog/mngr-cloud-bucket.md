Added R2 bucket support: a new `mngr imbue_cloud bucket` command group for
creating, listing, inspecting, and destroying R2 buckets (one per host, paid
accounts only), plus `bucket keys create/list/destroy` for minting and revoking
scoped S3 keys (read-only or read-write) to hand to different agents.

`bucket create` returns S3-compatible credentials (access key id, secret access
key, endpoint, bucket name) as JSON; the secret is shown only once and is never
stored by the service. `bucket destroy` refuses a non-empty bucket and, on
success, revokes all of that bucket's keys.
