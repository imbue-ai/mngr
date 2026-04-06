from imbue.imbue_common.primitives import NonEmptyStr


class CloudflareAccountId(NonEmptyStr):
    """A Cloudflare account ID."""

    ...


class CloudflareZoneId(NonEmptyStr):
    """A Cloudflare zone ID."""

    ...


class CloudflareTunnelId(NonEmptyStr):
    """A Cloudflare tunnel UUID."""

    ...


class CloudflareDnsRecordId(NonEmptyStr):
    """A Cloudflare DNS record ID."""

    ...


class TunnelName(NonEmptyStr):
    """Name of a Cloudflare tunnel, formatted as '{username}--{agent_id}'."""

    ...


class ServiceName(NonEmptyStr):
    """User-specified name for a forwarded service."""

    ...


class ServiceUrl(NonEmptyStr):
    """URL of the local service to forward to (e.g. 'http://localhost:8080')."""

    ...


class AgentId(NonEmptyStr):
    """An mngr agent ID passed in by the caller."""

    ...


class Username(NonEmptyStr):
    """An authenticated username."""

    ...


class DomainName(NonEmptyStr):
    """A domain name (e.g. 'example.com')."""

    ...
