# Setup

One-time. Everything after this is `minds-evals`.

## 1. Authenticate AWS (admin keys, your machine only)

Use keys that can create a bucket and an IAM user. These stay on your machine and are never given
to a sandbox.

```
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-1
aws sts get-caller-identity        # confirms they work; prints the account id
```

## 2. Create the bucket

Bucket names are globally unique; suffix with the account id.

```
BUCKET=minds-eval-backups-<account-id>
aws s3 mb "s3://$BUCKET" --region us-east-1
```

## 3. Create a scoped key for the sandboxes

Every eval sandbox gets these keys (restic writes snapshots to S3 from inside the workspace), and
the agent in there runs arbitrary code and can read them. So they must reach that one bucket and
nothing else -- never the admin keys from step 1.

```
aws iam create-user --user-name minds-eval-sandbox

aws iam put-user-policy --user-name minds-eval-sandbox --policy-name minds-eval-s3 \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",
  \"Action\":[\"s3:PutObject\",\"s3:GetObject\",\"s3:ListBucket\",\"s3:DeleteObject\"],
  \"Resource\":[\"arn:aws:s3:::$BUCKET\",\"arn:aws:s3:::$BUCKET/*\"]}]}"

aws iam create-access-key --user-name minds-eval-sandbox    # secret is shown ONCE
```

## 4. Write the credentials file

The CLI reads this and mounts it into the box read-only.

```
mkdir -p ~/.minds-eval
cat > ~/.minds-eval/aws.env <<EOF
AWS_ACCESS_KEY_ID=<from step 3>
AWS_SECRET_ACCESS_KEY=<from step 3>
AWS_DEFAULT_REGION=us-east-1
MINDS_EVAL_BUCKET=$BUCKET
EOF
chmod 600 ~/.minds-eval/aws.env
```

Verify the scoped key is actually scoped:

```
set -a; . ~/.minds-eval/aws.env; set +a
echo ok | aws s3 cp - "s3://$MINDS_EVAL_BUCKET/.test" && aws s3 rm "s3://$MINDS_EVAL_BUCKET/.test"
aws s3 ls        # must print nothing (denied outside the bucket)
```

## 5. Anthropic key

Needed by `launch` (the workspaces run with `ai_provider=api_key`).

```
export ANTHROPIC_API_KEY=sk-ant-...
```

See README.md to run an eval.
