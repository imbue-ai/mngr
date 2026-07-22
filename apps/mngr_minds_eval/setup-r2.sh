#!/usr/bin/env bash
# Create the eval's R2 bucket + scoped key and write ~/.minds-eval/r2.env from the output.
# Usage: ./setup-r2.sh [bucket-name]   (default: minds-eval-backups)
#
# Re-run note: `bucket create` fails if the bucket already exists (the secret is only shown once at
# creation). If you already have the bucket, mint a fresh key with
#   mngr imbue_cloud bucket keys create <bucket-name>
# and rerun the python block below by hand, or just destroy + recreate.
set -euo pipefail

NAME="${1:-minds-eval-backups}"
OUT="$(mngr imbue_cloud bucket create "$NAME")"

ENV_PATH="$(python3 - "$OUT" <<'PY'
import json, sys, pathlib
key = json.loads(sys.argv[1])["key"]
path = pathlib.Path.home() / ".minds-eval" / "r2.env"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(
    "AWS_ACCESS_KEY_ID={access_key_id}\n"
    "AWS_SECRET_ACCESS_KEY={secret_access_key}\n"
    "MINDS_EVAL_BUCKET={bucket_name}\n"
    "MINDS_EVAL_S3_ENDPOINT={s3_endpoint}\n".format(**key)
)
path.chmod(0o600)
print(path)
PY
)"

echo "wrote $ENV_PATH (bucket: $NAME)"
