Removed the now-unused `TagLimitExceededError`. It existed only to flag the EC2 50-tag ceiling for the AWS provider's offline tag mirror, which has been removed in favor of the S3 state bucket.
