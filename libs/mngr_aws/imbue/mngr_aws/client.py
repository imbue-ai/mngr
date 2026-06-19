import os
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final
from typing import assert_never

import boto3
from botocore.exceptions import ClientError
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import MngrError
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_aws.config import AutoCreateSecurityGroup
from imbue.mngr_aws.config import ExistingSecurityGroup
from imbue.mngr_aws.config import SecurityGroupSpec
from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.errors import VpsProvisioningError
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import VpsInstanceStatus
from imbue.mngr_vps.vps_client import VpsClientInterface

# Tag that ``create_instance`` adds to every EC2 instance launched while
# ``PYTEST_CURRENT_TEST`` is set. The conftest session-end scanner uses
# this tag (not the Name tag) to find leaked instances, which means tests
# do not have to constrain host naming: any agent name works.
AWS_PYTEST_LAUNCHED_TAG: Final[str] = "mngr-pytest-launched"

_STATE_MAP: Final[dict[str, VpsInstanceStatus]] = {
    "pending": VpsInstanceStatus.PENDING,
    "running": VpsInstanceStatus.ACTIVE,
    "stopping": VpsInstanceStatus.HALTED,
    "stopped": VpsInstanceStatus.HALTED,
    "shutting-down": VpsInstanceStatus.DESTROYING,
    "terminated": VpsInstanceStatus.DESTROYING,
}


class SecurityGroupPrepareResult(FrozenModel):
    """Outcome of ``AwsVpsClient.ensure_security_group`` / ``mngr aws prepare``."""

    security_group_id: str = Field(description="Id of the security group new instances will be attached to")
    was_created: bool = Field(
        description=(
            "True if a new security group was created by this call; False if it already existed "
            "(idempotent re-run, ingress re-authorized) or was a caller-supplied ExistingSecurityGroup"
        )
    )


class AwsVpsClient(VpsClientInterface):
    """EC2 client implementing the VPS provider interface via boto3."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # EC2 instance bootstrap is routinely slower than Vultr (Debian + Docker
    # install on a t3.small typically lands around 60-90s); raise the warning
    # threshold so normal boots don't log a "slow" warning.
    slow_provisioning_warning_threshold_seconds: float = Field(default=90.0)

    session: boto3.Session = Field(frozen=True, description="boto3 Session with resolved credentials")
    region: str = Field(frozen=True, description="AWS region this client targets")
    ami_id: str = Field(
        default="",
        frozen=True,
        description=(
            "Fallback AMI ID used when ``create_instance`` is invoked without an "
            "``ami_id_override``. The production code path always supplies an override "
            "(``AwsProvider._create_vps_instance`` resolves the AMI just-in-time from "
            "``AwsProviderConfig.get_ami_id_for_region``), so this defaults to empty "
            "and ``create_instance`` raises if neither source supplies one. Kept as a "
            "field so tests can pin a known AMI without going through the resolution "
            "path."
        ),
    )
    security_group: SecurityGroupSpec = Field(
        default_factory=AutoCreateSecurityGroup,
        description=(
            "Tagged union: ``ExistingSecurityGroup(id=...)`` to attach an existing SG, or "
            "``AutoCreateSecurityGroup(name=...)`` to look up / create one by name. Default "
            "is auto-create with the conventional 'mngr-aws' name."
        ),
    )
    subnet_id: str | None = Field(default=None, description="Subnet ID, or None to let EC2 pick a default")
    vpc_id: str | None = Field(default=None, description="VPC ID, used only to scope SG lookup")
    allowed_ssh_cidrs: tuple[str, ...] = Field(
        default=("0.0.0.0/0",),
        description=(
            "Inbound (ingress) CIDRs for tcp/22 and the container SSH port on the "
            "auto-created security group. Default ('0.0.0.0/0',) allows any IP; use e.g. "
            "('203.0.113.4/32',) to restrict, or () for no ingress. A warning is logged "
            "when the effective range is 0.0.0.0/0 or empty."
        ),
    )
    associate_public_ip: bool = Field(default=True, description="Assign a public IPv4 to launched instances")
    root_volume_size_gb: int = Field(default=30, description="Root EBS volume size in GB")
    root_volume_type: str = Field(default="gp3", description="Root EBS volume type")
    iam_instance_profile: str | None = Field(default=None, description="IAM instance profile name to attach")
    terminate_on_shutdown: bool = Field(
        default=False,
        description=(
            "Sets EC2 InstanceInitiatedShutdownBehavior: False -> 'stop' (resumable idle-pause), "
            "True -> 'terminate' (ephemeral / self-cleaning). See AwsProviderConfig for details."
        ),
    )
    container_ssh_port: int = Field(
        default=2222, description="Port the container's sshd is exposed on (added to the SG)"
    )
    _cached_ec2_client: Any = PrivateAttr(default=None)

    def _ec2(self) -> Any:
        """Return the EC2 client, building and caching it from the session on first use."""
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

    def ensure_security_group(self) -> SecurityGroupPrepareResult:
        """Return the SG to attach to new instances, creating it if needed.

        ``security_group`` is a tagged union:
        - ``ExistingSecurityGroup(id=...)``: return the id as-is
          (``was_created=False``: nothing is created or modified).
        - ``AutoCreateSecurityGroup(name=...)``: look up by name (optionally
          scoped to ``vpc_id``); create if absent (``was_created`` reflects
          whether a new group was created). Open tcp/22 and
          tcp/``container_ssh_port`` to every CIDR in ``allowed_ssh_cidrs``.

        Empty ``allowed_ssh_cidrs`` means "no ingress rules added": the SG is
        created (or reused) but the instance is unreachable from outside its
        VPC. Logged as a warning rather than raised so the behavior matches
        the Vultr / OVH default (no provider-managed firewall) -- key-only
        SSH is what protects the host, not network ACLs. The default ingress
        of ``0.0.0.0/0`` is logged as a warning too, prompting production
        users to tighten it.

        Used by ``mngr aws prepare`` (one-time admin setup). The hot path
        in ``create_instance`` uses ``resolve_security_group_id`` instead,
        which is lookup-only and requires only RunInstances-style permissions.
        """
        match self.security_group:
            case ExistingSecurityGroup(id=sg_id):
                return SecurityGroupPrepareResult(security_group_id=sg_id, was_created=False)
            case AutoCreateSecurityGroup(name=sg_name):
                return self._ensure_auto_created_security_group(sg_name)
            case _ as unreachable:
                assert_never(unreachable)

    def resolve_security_group_id(self) -> str:
        """Look up the SG id without creating or modifying anything.

        Mirrors ``ensure_security_group`` but with no write API calls --
        callers only need ``ec2:DescribeSecurityGroups``. When the auto-create
        SG is missing, raises a ``MngrError`` pointing at ``mngr aws prepare``
        so a user with restricted IAM (RunInstances-only) gets a clear next
        step rather than an opaque AWS permission denial.
        """
        match self.security_group:
            case ExistingSecurityGroup(id=sg_id):
                return sg_id
            case AutoCreateSecurityGroup(name=sg_name):
                return self._lookup_security_group_id_or_raise(sg_name)
            case _ as unreachable:
                assert_never(unreachable)

    def delete_security_group(self) -> str | None:
        """Delete the auto-created security group, undoing ``ensure_security_group``.

        The inverse of ``mngr aws prepare``. Returns the deleted SG id, or
        ``None`` when no matching SG exists (idempotent: cleaning an
        already-clean region is a no-op). Only valid for the auto-create case --
        an externally-managed ``ExistingSecurityGroup`` is not mngr's to delete,
        so this raises rather than touching a user-owned resource.

        Needs ec2:DescribeSecurityGroups + ec2:DeleteSecurityGroup. AWS refuses
        with ``DependencyViolation`` if any network interface still references
        the SG, so callers must ensure all instances are terminated first (the
        ``mngr aws cleanup`` command checks for live instances before calling).
        """
        match self.security_group:
            case ExistingSecurityGroup(id=sg_id):
                raise MngrError(
                    f"Refusing to delete security group {sg_id!r}: it is configured as an "
                    "existing (externally-managed) security group, not auto-created by mngr. "
                    "If you really want it gone, delete it yourself."
                )
            case AutoCreateSecurityGroup(name=sg_name):
                existing = self._describe_security_groups_or_raise_on_multi_vpc(sg_name)
                if not existing:
                    return None
                sg_id = existing[0]["GroupId"]
                with self._translate_aws_errors():
                    self._ec2().delete_security_group(GroupId=sg_id)
                logger.info("Deleted security group {} in region {}", sg_id, self.region)
                return sg_id
            case _ as unreachable:
                assert_never(unreachable)

    def _describe_security_groups_or_raise_on_multi_vpc(self, sg_name: str) -> list[dict[str, Any]]:
        """Describe SGs matching ``sg_name`` (optionally vpc-scoped). Raise on multi-VPC name collision.

        Shared between the lookup-only ``_lookup_security_group_id_or_raise``
        path and the create-if-missing ``_ensure_auto_created_security_group``
        path so the filter shape, the describe call, and the multi-VPC error
        message stay defined in one place. The not-found case is left to the
        caller (the two paths handle it differently: lookup raises with a
        prepare hint, ensure proceeds to CreateSecurityGroup).
        """
        filters: list[dict[str, Any]] = [{"Name": "group-name", "Values": [sg_name]}]
        if self.vpc_id is not None:
            filters.append({"Name": "vpc-id", "Values": [self.vpc_id]})
        with self._translate_aws_errors():
            existing = self._ec2().describe_security_groups(Filters=filters).get("SecurityGroups", [])
        if len(existing) > 1:
            # EC2 enforces (group-name, vpc-id) uniqueness, so >1 result here
            # means our lookup spans multiple VPCs (vpc_id is None and there
            # are SGs with the same name in different VPCs). Refuse to guess
            # which one the user wanted -- pick a specific one explicitly.
            sg_descriptions = ", ".join(f"{g['GroupId']} (vpc={g.get('VpcId', '?')})" for g in existing)
            raise MngrError(
                f"Found {len(existing)} security groups named {sg_name!r}: {sg_descriptions}. "
                "Set vpc_id on the AWS provider config to scope the lookup, or pass an explicit "
                "security_group=ExistingSecurityGroup(id='sg-...')."
            )
        return existing

    def _lookup_security_group_id_or_raise(self, sg_name: str) -> str:
        existing = self._describe_security_groups_or_raise_on_multi_vpc(sg_name)
        if not existing:
            raise MngrError(
                f"AWS security group named {sg_name!r} does not exist in region {self.region!r}. "
                f"Run `mngr aws prepare --region {self.region}` once to create it "
                "(needs ec2:CreateSecurityGroup + ec2:AuthorizeSecurityGroupIngress), then "
                "retry the create with your usual RunInstances-only credentials. Alternatively, "
                "set security_group = {kind = 'existing', id = 'sg-...'} on the provider config "
                "to attach an SG you manage outside mngr."
            )
        return existing[0]["GroupId"]

    def _warn_about_cidrs_if_needed(self, sg_name: str) -> None:
        """Emit a one-line warning when the effective CIDR set is empty or 0.0.0.0/0.

        The two cases need different wording: empty means "no usable ingress"
        (instance unreachable), whereas 0.0.0.0/0 means "open to the internet"
        (default but worth flagging). Anything else is silent.
        """
        if not self.allowed_ssh_cidrs:
            logger.warning(
                "AWS allowed_ssh_cidrs is empty; auto-created security group {!r} will have no "
                "ingress rules and the instance will be unreachable from outside its VPC. Set "
                "allowed_ssh_cidrs on the provider config (e.g. ('203.0.113.4/32',)) to fix.",
                sg_name,
            )
            return
        if "0.0.0.0/0" in self.allowed_ssh_cidrs:
            logger.warning(
                "AWS allowed_ssh_cidrs includes 0.0.0.0/0; auto-created security group {!r} will "
                "permit SSH from the public internet.",
                sg_name,
            )

    def _ssh_ingress_already_authorized(self, security_group: dict[str, Any]) -> bool:
        """Return whether the SG already permits every required SSH ingress rule.

        Lets ``ensure_security_group`` skip the privileged
        ``AuthorizeSecurityGroupIngress`` write when nothing is missing, so a
        caller with only ``ec2:DescribeSecurityGroups`` can confirm the group
        is ready (the read-only-first path minds relies on for its auto-prepare
        with restricted-IAM keys). An empty ``allowed_ssh_cidrs`` requires no
        ingress, so it is trivially satisfied -- matching
        ``_authorize_ssh_ingress_idempotent``, which issues no call in that case.

        Checks both tcp/22 and tcp/``container_ssh_port``: each required port
        must have every CIDR in ``allowed_ssh_cidrs`` present across the group's
        ``IpPermissions`` (rules may be split across multiple permission
        entries, so the granted CIDRs are unioned per port before comparison).
        """
        if not self.allowed_ssh_cidrs:
            return True
        granted_cidrs_by_port: dict[int, set[str]] = {22: set(), self.container_ssh_port: set()}
        for permission in security_group.get("IpPermissions", []):
            if permission.get("IpProtocol") != "tcp":
                continue
            from_port = permission.get("FromPort")
            to_port = permission.get("ToPort")
            cidrs = {ip_range.get("CidrIp", "") for ip_range in permission.get("IpRanges", [])}
            for port in granted_cidrs_by_port:
                if from_port == port and to_port == port:
                    granted_cidrs_by_port[port].update(cidrs)
        required_cidrs = set(self.allowed_ssh_cidrs)
        return all(required_cidrs <= granted for granted in granted_cidrs_by_port.values())

    def _ensure_auto_created_security_group(self, sg_name: str) -> SecurityGroupPrepareResult:
        self._warn_about_cidrs_if_needed(sg_name)

        existing = self._describe_security_groups_or_raise_on_multi_vpc(sg_name)
        if existing:
            sg_id = existing[0]["GroupId"]
            # Read-only-first: when the group already permits every required
            # ingress rule, issue no write call at all, so a describe-only key
            # succeeds. Only fall through to the privileged authorize when a
            # rule is genuinely missing.
            if self._ssh_ingress_already_authorized(existing[0]):
                logger.debug("Security group {} already has the required SSH ingress; skipping authorize", sg_id)
                return SecurityGroupPrepareResult(security_group_id=sg_id, was_created=False)
            self._authorize_ssh_ingress_idempotent(sg_id)
            return SecurityGroupPrepareResult(security_group_id=sg_id, was_created=False)

        create_kwargs: dict[str, Any] = {
            "GroupName": sg_name,
            "Description": "Auto-created by mngr_aws for SSH access to managed instances",
        }
        if self.vpc_id is not None:
            create_kwargs["VpcId"] = self.vpc_id
        with self._translate_aws_errors():
            result = self._ec2().create_security_group(**create_kwargs)
        sg_id = result["GroupId"]
        logger.info("Created security group {} in region {}", sg_id, self.region)
        self._authorize_ssh_ingress_idempotent(sg_id)
        return SecurityGroupPrepareResult(security_group_id=sg_id, was_created=True)

    def _authorize_ssh_ingress_idempotent(self, sg_id: str) -> None:
        """Authorize ingress for SSH ports on the SG, swallowing duplicate errors.

        Each permission is authorized in its own API call so that a duplicate
        on one port (e.g., tcp/22 already authorized from a previous run) does
        not cause AWS to reject the entire batch and silently drop the other
        port. AWS rejects ``AuthorizeSecurityGroupIngress`` calls with
        ``InvalidPermission.Duplicate`` atomically — none of the permissions in
        the batch are added if any one is a duplicate.

        When ``allowed_ssh_cidrs`` is empty, skip the authorize calls entirely:
        AWS rejects an ``IpPermission`` with no source set (no IpRanges /
        Ipv6Ranges / UserIdGroupPairs / PrefixListIds) with
        ``InvalidParameterValue``, so issuing the call with empty ``IpRanges``
        is a real API error, not a no-op. The "no usable ingress" shape is the
        SG sitting with the AWS default of zero ingress rules, which is exactly
        what the caller gets by not issuing the call.
        """
        if not self.allowed_ssh_cidrs:
            logger.debug(
                "Skipping authorize_security_group_ingress on {}: allowed_ssh_cidrs is empty "
                "(the security group keeps its default of zero ingress rules)",
                sg_id,
            )
            return
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
                logger.debug(
                    "Skipping duplicate ingress rule on SG {} (tcp/{}): already authorized",
                    sg_id,
                    permission["FromPort"],
                )

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
        ami_id_override: str | None = None,
        spot: bool = False,
    ) -> VpsInstanceId:
        """Provision an EC2 instance.

        Uses ``ami_id_override`` when supplied (from ``--aws-ami=<ami-id>`` on
        ``mngr create``), otherwise the client's configured default
        (``self.ami_id``). When ``spot`` is True (from the presence-only
        ``--aws-spot`` build arg), passes ``InstanceMarketOptions={'MarketType':
        'spot'}`` to RunInstances so the host runs on EC2 spot capacity. AWS
        may reclaim spot instances with ~2 minutes' interruption notice; the
        host is terminated, not stopped, on reclaim. Opt-in only.

        Both kwargs are AWS-specific: they widen ``AwsVpsClient.create_instance``'s
        signature beyond the shared ``VpsClientInterface.create_instance`` contract,
        so providers reach them through ``self.aws_client.create_instance(...)``
        (via ``AwsProvider._create_vps_instance``) rather than the abstract
        interface.
        """
        if region != self.region:
            raise VpsApiError(
                400,
                f"Cross-region create not supported: client bound to {self.region!r}, "
                f"got region={region!r}. Instantiate a region-specific client.",
            )

        effective_ami_id = ami_id_override or self.ami_id
        if not effective_ami_id:
            raise VpsApiError(
                400,
                "AwsVpsClient.create_instance called without a usable AMI: neither "
                "ami_id_override nor self.ami_id is set. The production path resolves "
                "the AMI in AwsProvider._create_vps_instance and always supplies an "
                "override; if you see this from a test, pass ami_id_override=... or "
                "construct the client with ami_id=... explicitly.",
            )

        sg_id = self.resolve_security_group_id()

        tag_specs: list[dict[str, str]] = [{"Key": k, "Value": v} for k, v in tags.items()]
        tag_specs.append({"Key": "Name", "Value": label})
        tag_specs.append({"Key": "mngr-created-at", "Value": datetime.now(timezone.utc).isoformat()})
        # Mark instances launched during pytest so the conftest session-end
        # orphan scanner can identify and force-terminate any leaks
        # without having to constrain the agent / host name shape.
        if "PYTEST_CURRENT_TEST" in os.environ:
            tag_specs.append({"Key": AWS_PYTEST_LAUNCHED_TAG, "Value": "true"})

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
            "ImageId": effective_ami_id,
            "InstanceType": plan,
            "MinCount": 1,
            "MaxCount": 1,
            "UserData": user_data,
            "BlockDeviceMappings": block_device_mappings,
            "NetworkInterfaces": network_interfaces,
            # stop (resumable idle-pause) vs terminate (ephemeral / self-cleaning);
            # see AwsProviderConfig.terminate_on_shutdown. Governs BOTH the idle
            # watcher's poweroff and the auto_shutdown_seconds time-cap poweroff.
            "InstanceInitiatedShutdownBehavior": "terminate" if self.terminate_on_shutdown else "stop",
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
        # Attach an explicit operator-supplied IAM instance profile if configured.
        # mngr's idle self-stop no longer needs one: the watcher powers the host
        # off and InstanceInitiatedShutdownBehavior decides stop-vs-terminate, so
        # there is no default profile to attach (and no iam:PassRole requirement).
        if self.iam_instance_profile is not None:
            run_kwargs["IamInstanceProfile"] = {"Name": self.iam_instance_profile}
        if spot:
            # Default spot config: AWS sets max price to the on-demand price
            # automatically; the dev-host use case accepts any non-zero capacity.
            run_kwargs["InstanceMarketOptions"] = {"MarketType": "spot"}

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
            effective_ami_id,
        )
        return VpsInstanceId(instance_id)

    def set_instance_tags(self, instance_id: VpsInstanceId, tags: Mapping[str, str]) -> None:
        """Upsert tags on an existing instance (EC2 ``create_tags`` is an upsert).

        Used to re-stamp the cheap identity tags offline discovery reads (e.g.
        the ``Name`` tag after a rename) without touching the rest of the
        instance's tag set. AWS-only, like ``stop_instance`` -- reached via
        ``self.aws_client``, not the shared ``VpsClientInterface``.
        """
        with self._translate_aws_errors():
            self._ec2().create_tags(
                Resources=[str(instance_id)],
                Tags=[{"Key": key, "Value": value} for key, value in tags.items()],
            )

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        with self._translate_aws_errors():
            self._ec2().terminate_instances(InstanceIds=[str(instance_id)])
        logger.info("Terminated EC2 instance {}", instance_id)

    def stop_instance(self, instance_id: VpsInstanceId, timeout_seconds: float = 300.0) -> None:
        """Stop (not terminate) an EC2 instance, preserving its EBS volumes.

        Unlike ``destroy_instance`` (terminate), a stop keeps the root EBS
        volume and all on-disk state intact so the instance can later be
        resumed via ``start_instance``. Compute billing ends while stopped;
        EBS storage continues to bill. Blocks until the instance reaches the
        terminal ``stopped`` state so callers can rely on the volume being
        quiesced before, e.g., snapshotting or reading metadata. Idempotent:
        stopping an already-stopped instance still waits for ``stopped``.

        This method widens ``AwsVpsClient`` beyond the shared
        ``VpsClientInterface`` (which has no stop/start, since most VPS
        providers in this repo only support create/destroy); ``AwsProvider``
        reaches it through ``self.aws_client`` rather than the abstract
        interface.
        """
        with self._translate_aws_errors():
            self._ec2().stop_instances(InstanceIds=[str(instance_id)])
        logger.info("Stopping EC2 instance {}", instance_id)
        self._wait_for_instance_state(instance_id, "stopped", timeout_seconds)
        logger.info("EC2 instance {} stopped", instance_id)

    def start_instance(self, instance_id: VpsInstanceId, timeout_seconds: float = 300.0) -> str:
        """Start a previously-stopped EC2 instance and return its public IP.

        A stopped instance loses its public IPv4 address; AWS assigns a fresh
        one on start (unless an Elastic IP is associated), so the returned IP
        may differ from the pre-stop address -- callers must refresh any cached
        address / known_hosts entries. Reuses ``wait_for_instance_active`` to
        block until the instance is ``running`` and has a public IP. Idempotent:
        starting an already-running instance returns its current IP.

        AWS-only, like ``stop_instance`` -- reached via ``self.aws_client``.
        """
        # AWS rejects ``start-instances`` on an instance that is still ``stopping``
        # (``IncorrectInstanceState``). When resuming a host caught mid-stop -- e.g.
        # one the idle watcher just powered off, resolved during its stop transition
        # -- wait for the terminal ``stopped`` state before starting it.
        if self._instance_state_name(instance_id) == "stopping":
            self._wait_for_instance_state(instance_id, "stopped", timeout_seconds)
        with self._translate_aws_errors():
            self._ec2().start_instances(InstanceIds=[str(instance_id)])
        logger.info("Starting EC2 instance {}", instance_id)
        return self.wait_for_instance_active(instance_id, timeout_seconds=timeout_seconds)

    def _instance_state_name(self, instance_id: VpsInstanceId) -> str:
        """Return the raw EC2 state name (e.g. ``running`` / ``stopped``), or ``''`` if absent.

        ``''`` is returned only when the describe response contains no instance
        record; a truly-nonexistent instance id instead makes ``_describe_instance``
        raise ``VpsApiError`` (``InvalidInstanceID.NotFound``).

        Unlike ``get_instance_status``, this preserves the raw EC2 state name
        rather than collapsing ``stopping`` / ``stopped`` into a single
        ``HALTED`` status, so callers can distinguish "still stopping" from
        "fully stopped".
        """
        instance = self._describe_instance(instance_id)
        return instance.get("State", {}).get("Name", "") if instance is not None else ""

    def _wait_for_instance_state(
        self,
        instance_id: VpsInstanceId,
        target_state: str,
        timeout_seconds: float,
    ) -> None:
        """Poll ``describe_instances`` every 5s until the instance reaches ``target_state``.

        Used by ``stop_instance`` to wait for the terminal ``stopped`` state.
        Polls via the shared ``wait_for`` helper (not a raw ``time.sleep`` loop)
        and re-raises its ``TimeoutError`` as ``VpsProvisioningError`` to match
        the rest of this client's error contract.
        """
        try:
            wait_for(
                lambda: self._instance_state_name(instance_id) == target_state,
                timeout=timeout_seconds,
                poll_interval=5.0,
                error_message=(
                    f"EC2 instance {instance_id} did not reach state {target_state!r} within {timeout_seconds}s"
                ),
            )
        except TimeoutError as e:
            raise VpsProvisioningError(str(e)) from e

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

    def list_instances(self, provider_tag: str | None = None) -> list[dict[str, Any]]:
        """List instances in this region. Optionally filtered by ``mngr-provider=<value>`` tag.

        Returns a normalized list of dicts with keys: ``id``, ``main_ip``, ``state``,
        ``tags`` (a list of ``"key=value"`` strings to mirror Vultr's tag shape).
        """
        extra_filters: list[dict[str, Any]] = []
        if provider_tag is not None:
            extra_filters.append({"Name": "tag:mngr-provider", "Values": [provider_tag]})
        return self._list_active_instances(extra_filters)

    def list_mngr_managed_instances(self) -> list[dict[str, Any]]:
        """List non-terminated instances in this region carrying any ``mngr-provider`` tag.

        Filters by tag-*key* presence (any value), so it spans every mngr
        provider config bound to this region, not just one provider name. Used
        by ``mngr aws cleanup`` to refuse deleting the shared security group
        while any mngr-managed agent still exists in the region.
        """
        return self._list_active_instances([{"Name": "tag-key", "Values": ["mngr-provider"]}])

    def _list_active_instances(self, extra_filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Describe non-terminated instances in this region, normalized to dicts.

        Shared spine for ``list_instances`` and ``list_mngr_managed_instances``:
        both want the same state filter and the same ``id``/``main_ip``/``state``/
        ``tags`` normalization, differing only in the extra filter that scopes
        which instances count.
        """
        filters: list[dict[str, Any]] = [
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
            *extra_filters,
        ]
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
