from typing import Final

from botocore.config import Config

# botocore's registered name for the EC2 instance-metadata (IMDS) credential
# provider in the default credential resolver chain. Removing the provider by
# this name disables credential lookups against ``169.254.169.254`` without
# touching the rest of the chain (env vars, shared files, SSO, etc.).
IMDS_CREDENTIAL_PROVIDER_NAME: Final[str] = "iam-role"

# Bounded timeouts + retries applied to every AWS *service* client mngr builds
# (EC2 / STS / S3). boto3's defaults are a 60s connect and 60s read timeout with
# legacy retries, so a slow or blackholed AWS endpoint can hang for minutes;
# combined with discovery having no per-provider timeout, one stuck call can
# freeze a whole discovery snapshot. These caps make such a call fail in seconds.
# (This does NOT bound the IMDS *credential* probe -- that is the credential
# resolver, not a service client; see ``IMDS_CREDENTIAL_PROVIDER_NAME`` and
# ``AwsProviderConfig.use_ec2_instance_metadata`` for that path.)
AWS_BOTO_CONFIG: Final[Config] = Config(
    connect_timeout=5,
    read_timeout=15,
    # botocore's ``max_attempts`` is the number of *retries*, so 2 == the initial
    # try plus two retries (it normalizes to ``total_max_attempts: 3``).
    retries={"max_attempts": 2, "mode": "standard"},
)
