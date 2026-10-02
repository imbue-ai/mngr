"""Migrations of a policy that record what they were handed, for exercising the runner and the sync paths."""

from pydantic import Field

from imbue.imbue_common.model_update import to_update
from imbue.mngr_latchkey.migrations.interface import PermissionsMigration
from imbue.mngr_latchkey.migrations.interface import PermissionsMigrationError
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig


class RuleAppendingMigration(PermissionsMigration):
    """A migration that grants one rule, so a test can see that it ran and what it was handed."""

    rule_key: str = Field(description="The rule this migration adds to the policy, once.")
    applied_to: list[LatchkeyPermissionsConfig] = Field(
        default_factory=list, description="Every policy this was handed, in order."
    )

    def apply(self, permissions: LatchkeyPermissionsConfig) -> LatchkeyPermissionsConfig:
        self.applied_to.append(permissions)
        if any(self.rule_key in rule for rule in permissions.rules):
            return permissions
        return permissions.model_copy_update(
            to_update(permissions.field_ref().rules, (*permissions.rules, {self.rule_key: ["any"]}))
        )


class FailingMigration(PermissionsMigration):
    """A migration that never manages to run, for what the runner does with one that cannot."""

    def apply(self, permissions: LatchkeyPermissionsConfig) -> LatchkeyPermissionsConfig:
        raise PermissionsMigrationError(
            f"migration {self.version} cannot rewrite a policy with {len(permissions.rules)} rules"
        )
