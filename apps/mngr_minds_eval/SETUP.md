# Setup

One-time. Everything after this is `minds-evals`.

Results live in a Cloudflare R2 bucket. Your `mngr imbue_cloud` login already authorizes you to
create R2 buckets and scoped keys, so there is no AWS account, no IAM, and no admin credentials to
manage -- one command mints both the bucket and a bucket-scoped key.

## 1. Create the bucket + scoped key

```
mngr imbue_cloud bucket create minds-eval-backups
```

This prints JSON with everything you need -- the bucket, its S3-compatible endpoint, and a scoped
readwrite key (the `secret_access_key` is shown ONCE):

```json
{
  "bucket": {"bucket_name": "minds-eval-backups", "s3_endpoint": "https://<account>.r2.cloudflarestorage.com", ...},
  "key": {
    "access_key_id": "...",
    "secret_access_key": "...",
    "s3_endpoint": "https://<account>.r2.cloudflarestorage.com",
    "bucket_name": "minds-eval-backups",
    "access": "readwrite"
  }
}
```

The key is already scoped to just this bucket, so it is safe to hand to the eval sandboxes (restic
writes snapshots to R2 from inside the workspace, where the agent runs arbitrary code and can read
the key). It can reach that one bucket and nothing else.

## 2. Write the credentials file

The CLI reads this and mounts it into the box read-only. Fill in the four values from the `key`
object above (R2 uses the standard `AWS_*` names -- that is what boto3 and restic read).

```
mkdir -p ~/.minds-eval
cat > ~/.minds-eval/r2.env <<EOF
AWS_ACCESS_KEY_ID=<key.access_key_id>
AWS_SECRET_ACCESS_KEY=<key.secret_access_key>
MINDS_EVAL_BUCKET=<key.bucket_name>
MINDS_EVAL_S3_ENDPOINT=<key.s3_endpoint>
EOF
chmod 600 ~/.minds-eval/r2.env
```

(No region -- R2 ignores it; the CLI defaults it to `auto`.)

## 3. Anthropic key

Needed by `launch` (the workspaces run with `ai_provider=api_key`).

```
export ANTHROPIC_API_KEY=sk-ant-...
```

See README.md to run an eval.
