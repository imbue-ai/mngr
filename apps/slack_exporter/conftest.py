from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.slack_exporter.data_types import ChannelConfig
from imbue.slack_exporter.data_types import ExporterSettings
from imbue.slack_exporter.primitives import SlackChannelName

register_conftest_hooks(globals())


@pytest.fixture()
def temp_output_dir(tmp_path: Path) -> Path:
    return tmp_path / "slack_export"


@pytest.fixture()
def default_settings(temp_output_dir: Path) -> ExporterSettings:
    return ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
        max_recent_threads_for_reactions=0,
        cache_ttl_seconds=0,
    )
