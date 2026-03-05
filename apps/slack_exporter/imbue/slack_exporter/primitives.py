from imbue.imbue_common.primitives import NonEmptyStr


class SlackChannelId(NonEmptyStr):
    """A Slack channel ID (e.g. 'CT5TK805S')."""

    ...


class SlackChannelName(NonEmptyStr):
    """A Slack channel name without the leading '#' (e.g. 'general')."""

    ...


class SlackMessageTimestamp(NonEmptyStr):
    """A Slack message timestamp used for pagination (e.g. '1234567890.123456')."""

    ...


class SlackUserId(NonEmptyStr):
    """A Slack user ID (e.g. 'U01ABCDEF')."""

    ...
