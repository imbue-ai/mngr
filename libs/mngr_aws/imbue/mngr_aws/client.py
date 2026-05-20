import os
import time
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final

import boto3
from botocore.exceptions import ClientError
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.mngr.errors import MngrError
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsProvisioningError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vps_docker.primitives import VpsSnapshotId
from imbue.mngr_vps_docker.vps_client import VpsClientInterface
from imbue.mngr_vps_docker.vps_client import VpsSnapshotInfo
from imbue.mngr_vps_docker.vps_client import VpsSshKeyInfo

# Prefix the conftest hook scans for to find leaked test instances. Tests
# under pytest must produce EC2 `Name` tags that begin with this prefix so
# the session-end orphan scan can find them. Defined here in production
# code (not in ``mngr_aws.testing``) because the production guard in
# ``create_instance`` depends on it; ``mngr_aws.testing`` imports this
# constant and derives its ``AWS_TEST_NAME_PREFIX`` from it, so the two
# can never drift.
AWS_TEST_INSTANCE_LABEL_PREFIX: Final[str] = "mngr-test-aws-"

_STATE_MAP: Final[dict[str, VpsInstanceStatus]] = {
    "pending": VpsInstanceStatus.PENDING,
    "running": VpsInstanceStatus.ACTIVE,
    "stopping": VpsInstanceStatus.HALTED,
    "stopped": VpsInstanceStatus.HALTED,
    "shutting-down": VpsInstanceStatus.DESTROYING,
    "terminated": VpsInstanceStatus.DESTROYING,
}


class AwsVpsClient(VpsClientInterface):
    """EC2 client implementing the VPS provider interface via boto3."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: boto3.Session = Field(frozen=True, description="boto3 Session with resolved credentials")
    region: str = Field(frozen=True, description="AWS region this client targets")
    ami_id: str = Field(frozen=True, description="Default AMI ID for instances created via this client")
    security_group_id: str | None = Field(
        default=None, description="Security group attached to launched instances; auto-created if None"
    )
    security_group_name: str = Field(default="mngr-aws", description="Name used when auto-creating the security group")
    subnet_id: str | None = Field(default=None, description="Subnet ID, or None to let EC2 pick a default")
    vpc_id: str | None = Field(default=None, description="VPC ID, used only to scope SG lookup")
    allowed_ssh_cidrs: tuple[str, ...] = Field(
        default=(),
        description=(
            "CIDR blocks allowed inbound on tcp/22 and tcp/container_ssh_port of the auto-created "
            "security group. Empty by default (fail-closed): if no security_group_id is provided "
            "and this is empty, ensure_security_group raises rather than creating a wide-open SG. "
            "Set to e.g. ('203.0.113.4/32',) to restrict to your own IP, or ('0.0.0.0/0',) to "
            "expose to the public internet (NOT recommended for production)."
        ),
    )
    associate_public_ip: bool = Field(default=True, description="Assign a public IPv4 to launched instances")
    root_volume_size_gb: int = Field(default=30, description="Root EBS volume size in GB")
    root_volume_type: str = Field(default="gp3", description="Root EBS volume type")
    iam_instance_profile: str | None = Field(default=None, description="IAM instance profile name to attach")
    container_ssh_port: int = Field(
        default=2222, description="Port the container's sshd is exposed on (added to the SG)"
    )
    ec2_client: Any | None = Field(
        default=None,
        description=(
            "Optional pre-built EC2 client (e.g. a botocore Stubber-wrapped client for tests). "
            "When None, the client is lazily built from ``session`` on first use and cached."
        ),
    )

    _cached_ec2_client: Any = PrivateAttr(default=None)

    def _ec2(self) -> Any:
        if self.ec2_client is not None:
            return self.ec2_client
        if self._cached_ec2_client is None:
            self._cached_ec2_client = self.session.client("ec2", region_name=self.region)
        return self._cached_ec2_client

    @contextmanager
    def _translate_aws_errors(self) -> Iterator[None]:
        """Translate ``botocore.exceptions.ClientError`` into ``VpsApiError`` while inside the block."""
        try:
            yield
        except ClientError as e:
            err = e.response.get("Error", {})
            code = err.get("Code", "Unknown")
            message = err.get("Message", str(e))
            http_status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            raise VpsApiError(http_status, f"{code}: {message}") from e

    # =========================================================================
    # Security group management (idempotent)
    # =========================================================================

    def ensure_security_group(self) -> str:
        """Return the SG id to attach to new instances, creating it if needed.

        If ``security_group_id`` was provided, it is returned as-is. Otherwise
        an SG named ``security_group_name`` is looked up (optionally scoped to
        ``vpc_id``) and created if absent, with ingress for tcp/22 and
        tcp/``container_ssh_port`` from every CIDR in ``allowed_ssh_cidrs``.

        Fails closed if ``allowed_ssh_cidrs`` is empty: rather than create a
        no-ingress SG (which would result in unreachable instances) or default
        to ``0.0.0.0/0`` (which would expose SSH to the public internet),
        we raise so the caller has to make an explicit decision. Matches AWS's
        own native default for a brand-new SG: no ingress until you add it.
        """
        if self.security_group_id is not None:
            return self.security_group_id

        if not self.allowed_ssh_cidrs:
            raise MngrError(
                "Cannot auto-create an AWS security group: allowed_ssh_cidrs is empty. "
                "Either set allowed_ssh_cidrs to a tuple of CIDR blocks (e.g. ('203.0.113.4/32',) "
                "for your own IP), or pre-create the SG and pass its id as security_group_id."
            )

        filters: list[dict[str, Any]] = [{"Name": "group-name", "Values": [self.security_group_name]}]
        if self.vpc_id is not None:
            filters.append({"Name": "vpc-id", "Values": [self.vpc_id]})

        with self._translate_aws_errors():
            existing = self._ec2().describe_security_groups(Filters=filters).get("SecurityGroups", [])
        if existing:
            sg_id = existing[0]["GroupId"]
            self._authorize_ssh_ingress_idempotent(sg_id)
            return sg_id

        create_kwargs: dict[str, Any] = {
            "GroupName": self.security_group_name,
            "Description": "Auto-created by mngr_aws for SSH access to managed instances",
        }
        if self.vpc_id is not None:
            create_kwargs["VpcId"] = self.vpc_id
        with self._translate_aws_errors():
            result = self._ec2().create_security_group(**create_kwargs)
        sg_id = result["GroupId"]
        logger.info("Created security group {} in region {}", sg_id, self.region)
        self._authorize_ssh_ingress_idempotent(sg_id)
        return sg_id

    def _authorize_ssh_ingress_idempotent(self, sg_id: str) -> None:
        """Authorize ingress for SSH ports on the SG, swallowing duplicate errors.

        Each permission is authorized in its own API call so that a duplicate
        on one port (e.g., tcp/22 already authorized from a previous run) does
        not cause AWS to reject the entire batch and silently drop the other
        port. AWS rejects ``AuthorizeSecurityGroupIngress`` calls with
        ``InvalidPermission.Duplicate`` atomically — none of the permissions in
        the batch are added if any one is a duplicate.
        """
        ip_ranges = [{"CidrIp": cidr} for cidr in self.allowed_ssh_cidrs]
        ip_permissions = [
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": ip_ranges,
            },
            {
                "IpProtocol": "tcp",
                "FromPort": self.container_ssh_port,
                "ToPort": self.container_ssh_port,
                "IpRanges": ip_ranges,
            },
        ]
        for permission in ip_permissions:
            try:
                self._ec2().authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[permission])
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                # InvalidPermission.Duplicate means "already authorized" -- treat as success (idempotent ensure).
                if code != "InvalidPermission.Duplicate":
                    http_status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
                    raise VpsApiError(http_status, f"{code}: {e}") from e

    # =========================================================================
    # Instance Operations
    # =========================================================================

    def create_instance(
        self,
        label: str,
        region: str,
        plan: str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Mapping[str, str],
    ) -> VpsInstanceId:
        """Provision an EC2 instance using the client's configured AMI."""
        if region != self.region:
            raise VpsApiError(
                400,
                f"Cross-region create not supported: client bound to {self.region!r}, "
                f"got region={region!r}. Instantiate a region-specific client.",
            )

        # Mirrors the Modal pattern in ``mngr_modal.backend._create_environment``:
        # under pytest, refuse to launch an instance whose Name tag the
        # conftest leak scanner could not later identify as test-owned.
        # Without this, a test that spawns ``mngr create`` via an in-process
        # code path that inherits os.environ but forgets to override the
        # host name would create a default-prefixed instance no cleanup
        # script recognises.
        if "PYTEST_CURRENT_TEST" in os.environ and not label.startswith(AWS_TEST_INSTANCE_LABEL_PREFIX):
            raise MngrError(
                f"Refusing to create EC2 instance with label {label!r} during pytest: "
                f"test instance labels must start with {AWS_TEST_INSTANCE_LABEL_PREFIX!r} so the "
                "session-end orphan scan in mngr_aws/conftest.py can find leaked "
                "instances by Name tag."
            )

        sg_id = self.ensure_security_group()

        tag_specs: list[dict[str, str]] = [{"Key": k, "Value": v} for k, v in tags.items()]
        tag_specs.append({"Key": "Name", "Value": label})
        tag_specs.append({"Key": "mngr-created-at", "Value": datetime.now(timezone.utc).isoformat()})

        block_device_mappings = [
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {
                    "VolumeSize": self.root_volume_size_gb,
                    "VolumeType": self.root_volume_type,
                    "DeleteOnTermination": True,
                    # Explicit Encrypted=True so encryption-at-rest is guaranteed
                    # regardless of the AWS account's EBS-default-encryption
                    # setting (which only became automatic on new accounts in
                    # 2023). Uses the account's default KMS key.
                    "Encrypted": True,
                },
            }
        ]

        network_interfaces: list[dict[str, Any]] = [
            {
                "DeviceIndex": 0,
                "AssociatePublicIpAddress": self.associate_public_ip,
                "Groups": [sg_id],
                "DeleteOnTermination": True,
            }
        ]
        if self.subnet_id is not None:
            network_interfaces[0]["SubnetId"] = self.subnet_id

        run_kwargs: dict[str, Any] = {
            "ImageId": self.ami_id,
            "InstanceType": plan,
            "MinCount": 1,
            "MaxCount": 1,
            "UserData": user_data,
            "BlockDeviceMappings": block_device_mappings,
            "NetworkInterfaces": network_interfaces,
            "InstanceInitiatedShutdownBehavior": "terminate",
            # IMDSv2 required: refuse IMDSv1 (unauthenticated GET) entirely
            # and cap the response-hop limit at 1 so the metadata service
            # cannot be reached from a hostile container running on the
            # instance. Mirrors the AWS-recommended secure default from
            # https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-service.html.
            "MetadataOptions": {
                "HttpTokens": "required",
                "HttpEndpoint": "enabled",
                "HttpPutResponseHopLimit": 1,
            },
            "TagSpecifications": [
                {"ResourceType": "instance", "Tags": tag_specs},
                {"ResourceType": "volume", "Tags": tag_specs},
            ],
        }
        if ssh_key_ids:
            run_kwargs["KeyName"] = ssh_key_ids[0]
        if self.iam_instance_profile is not None:
            run_kwargs["IamInstanceProfile"] = {"Name": self.iam_instance_profile}

        with self._translate_aws_errors():
            result = self._ec2().run_instances(**run_kwargs)
        instances = result.get("Instances", [])
        if not instances:
            raise VpsProvisioningError("RunInstances returned no instances")
        instance_id = instances[0]["InstanceId"]
        logger.info(
            "Created EC2 instance {} (label: {}, region: {}, type: {}, ami: {})",
            instance_id,
            label,
            region,
            plan,
            self.ami_id,
        )
        return VpsInstanceId(instance_id)

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        with self._translate_aws_errors():
            self._ec2().terminate_instances(InstanceIds=[str(instance_id)])
        logger.info("Terminated EC2 instance {}", instance_id)

    def _describe_instance(self, instance_id: VpsInstanceId) -> dict[str, Any] | None:
        with self._translate_aws_errors():
            result = self._ec2().describe_instances(InstanceIds=[str(instance_id)])
        for reservation in result.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                return instance
        return None

    def get_instance_status(self, instance_id: VpsInstanceId) -> VpsInstanceStatus:
        try:
            instance = self._describe_instance(instance_id)
        except VpsApiError as e:
            # Only the specific "instance does not exist" AWS code should be
            # treated as UNKNOWN. Other ``*.NotFound`` codes (e.g.,
            # InvalidSubnetID.NotFound) indicate real misconfiguration and
            # must surface to the caller.
            if "InvalidInstanceID.NotFound" in str(e):
                return VpsInstanceStatus.UNKNOWN
            raise
        if instance is None:
            return VpsInstanceStatus.UNKNOWN
        state = instance.get("State", {}).get("Name", "")
        return _STATE_MAP.get(state, VpsInstanceStatus.UNKNOWN)

    def get_instance_ip(self, instance_id: VpsInstanceId) -> str:
        instance = self._describe_instance(instance_id)
        if instance is None:
            raise VpsApiError(404, f"Instance {instance_id} not found")
        ip = instance.get("PublicIpAddress", "")
        if not ip:
            raise VpsProvisioningError(f"Instance {instance_id} does not have a public IP yet")
        return ip

    def wait_for_instance_active(
        self,
        instance_id: VpsInstanceId,
        timeout_seconds: float = 300.0,
    ) -> str:
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            status = self.get_instance_status(instance_id)
            if status == VpsInstanceStatus.ACTIVE:
                try:
                    ip = self.get_instance_ip(instance_id)
                    elapsed = time.monotonic() - start
                    if elapsed > 90.0:
                        logger.warning("EC2 provisioning took {:.1f}s (threshold: 90s)", elapsed)
                    return ip
                except VpsProvisioningError:
                    pass
            time.sleep(5.0)
        raise VpsProvisioningError(f"EC2 instance {instance_id} did not become active within {timeout_seconds}s")

    def list_instances(self, provider_tag: str | None = None) -> list[dict[str, Any]]:
        """List instances in this region. Optionally filtered by ``mngr-provider=<value>`` tag.

        Returns a normalized list of dicts with keys: ``id``, ``main_ip``, ``state``,
        ``tags`` (a list of ``"key=value"`` strings to mirror Vultr's tag shape).
        """
        filters: list[dict[str, Any]] = [
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}
        ]
        if provider_tag is not None:
            filters.append({"Name": "tag:mngr-provider", "Values": [provider_tag]})

        instances: list[dict[str, Any]] = []
        # The paginator defers actual API calls until iteration, so the error
        # translation block must wrap the iteration itself, not just the
        # ``get_paginator`` factory call.
        with self._translate_aws_errors():
            paginator = self._ec2().get_paginator("describe_instances")
            for page in paginator.paginate(Filters=filters):
                for reservation in page.get("Reservations", []):
                    for instance in reservation.get("Instances", []):
                        tag_kv = [f"{t['Key']}={t['Value']}" for t in instance.get("Tags", [])]
                        instances.append(
                            {
                                "id": instance.get("InstanceId", ""),
                                "main_ip": instance.get("PublicIpAddress", ""),
                                "state": instance.get("State", {}).get("Name", ""),
                                "tags": tag_kv,
                            }
                        )
        return instances

    # =========================================================================
    # Snapshot Operations (EBS root volume of an instance)
    # =========================================================================

    def _get_root_volume_id(self, instance_id: VpsInstanceId) -> str:
        instance = self._describe_instance(instance_id)
        if instance is None:
            raise VpsApiError(404, f"Instance {instance_id} not found")
        for mapping in instance.get("BlockDeviceMappings", []):
            ebs = mapping.get("Ebs", {})
            if ebs.get("VolumeId"):
                return ebs["VolumeId"]
        raise VpsApiError(500, f"Instance {instance_id} has no EBS volume")

    def create_snapshot(self, instance_id: VpsInstanceId, description: str) -> VpsSnapshotId:
        volume_id = self._get_root_volume_id(instance_id)
        with self._translate_aws_errors():
            result = self._ec2().create_snapshot(VolumeId=volume_id, Description=description)
        snap_id = result.get("SnapshotId", "")
        if not snap_id:
            raise VpsApiError(500, "CreateSnapshot returned no SnapshotId")
        logger.info("Created EBS snapshot {} for volume {}", snap_id, volume_id)
        return VpsSnapshotId(snap_id)

    def delete_snapshot(self, snapshot_id: VpsSnapshotId) -> None:
        with self._translate_aws_errors():
            self._ec2().delete_snapshot(SnapshotId=str(snapshot_id))
        logger.info("Deleted EBS snapshot {}", snapshot_id)

    def list_snapshots(self) -> list[VpsSnapshotInfo]:
        with self._translate_aws_errors():
            result = self._ec2().describe_snapshots(OwnerIds=["self"])
        snapshots: list[VpsSnapshotInfo] = []
        for snap in result.get("Snapshots", []):
            created = snap.get("StartTime")
            if isinstance(created, datetime):
                created_at = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
            else:
                created_at = datetime.now(timezone.utc)
            snapshots.append(
                VpsSnapshotInfo(
                    id=VpsSnapshotId(snap["SnapshotId"]),
                    description=snap.get("Description", ""),
                    created_at=created_at,
                )
            )
        return snapshots

    # =========================================================================
    # SSH Key Operations (EC2 KeyPairs)
    # =========================================================================

    def upload_ssh_key(self, name: str, public_key: str) -> str:
        """Import an SSH public key as an EC2 KeyPair. Returns the KeyName as the ID."""
        with self._translate_aws_errors():
            self._ec2().import_key_pair(KeyName=name, PublicKeyMaterial=public_key.encode("utf-8"))
        logger.debug("Imported EC2 KeyPair {}", name)
        return name

    def delete_ssh_key(self, key_id: str) -> None:
        with self._translate_aws_errors():
            self._ec2().delete_key_pair(KeyName=key_id)
        logger.debug("Deleted EC2 KeyPair {}", key_id)

    def list_ssh_keys(self) -> list[VpsSshKeyInfo]:
        with self._translate_aws_errors():
            result = self._ec2().describe_key_pairs()
        return [VpsSshKeyInfo(id=k["KeyName"], name=k["KeyName"]) for k in result.get("KeyPairs", [])]
