from datetime import datetime
from enum import auto

from pydantic import AliasChoices
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel


class ScheduledMngCommand(UpperCaseStrEnum):
    """The mng commands that can be scheduled."""

    CREATE = auto()
    START = auto()
    MESSAGE = auto()
    EXEC = auto()


class VerifyMode(UpperCaseStrEnum):
    """Controls post-deploy verification behavior."""

    NONE = auto()
    QUICK = auto()
    FULL = auto()


class ScheduleTriggerDefinition(FrozenModel):
    """A scheduled trigger that runs an mng command on a cron schedule."""

    name: str = Field(description="Unique name for this scheduled trigger")
    command: ScheduledMngCommand = Field(description="Which mng command to run")
    args: str = Field(default="", description="Arguments to pass to the mng command")
    schedule_cron: str = Field(description="Cron expression defining when the command runs")
    provider: str = Field(description="Provider on which to run the scheduled command (e.g. 'modal')")
    is_enabled: bool = Field(default=True, description="Whether this schedule is active")
    git_image_hash: str = Field(default="", description="Git commit SHA for packaging project code into the image")


class ScheduleCreationRecord(FrozenModel):
    """Metadata about how a scheduled trigger was created.

    Base class for all providers. Provider-specific subclasses (e.g.
    ModalScheduleCreationRecord) add additional fields.
    """

    trigger: ScheduleTriggerDefinition = Field(description="The trigger definition that was deployed")
    full_commandline: str = Field(description="The full command line used to create this schedule")
    hostname: str = Field(description="The hostname of the machine where the schedule was created")
    working_directory: str = Field(description="The directory from which the schedule was created")
    mng_git_hash: str = Field(description="Git commit hash of the mng codebase at creation time")
    created_at: datetime = Field(description="UTC timestamp of when the schedule was created")


class ModalScheduleCreationRecord(ScheduleCreationRecord):
    """Schedule creation record with Modal-specific metadata."""

    app_name: str = Field(
        description="The Modal app name for this schedule",
        validation_alias=AliasChoices("app_name", "modal_app_name"),
    )
    environment: str = Field(
        description="The Modal environment name",
        validation_alias=AliasChoices("environment", "modal_environment"),
    )
