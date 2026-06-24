"""A stashed, not-yet-started workspace creation, resumed after sign-in.

When a signed-out user chooses the remote (Imbue Cloud) preset and presses
"Create", the desktop client cannot create the workspace yet -- the Imbue
Cloud path needs an account. Rather than discard their selections, the create
handler captures them as a :class:`PendingCreateParams` and stashes it on the
app state, then sends the user into the sign-in/up flow. On a successful
sign-in the ``/create/resume`` route pops the stash and starts creation with
exactly those selections, so the user lands on the creating page without
re-filling the form.

The stash holds the already-parsed/validated form values (the resolved host
name and color, not the raw inputs). The account is intentionally *not* stored:
it does not exist yet at stash time, so the resume step resolves the
just-signed-in account itself.
"""

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import BackupEncryptionMethod
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import LaunchMode


class PendingCreateParams(FrozenModel):
    """The selections from a submitted create form, awaiting a sign-in to run.

    Mirrors the inputs ``_handle_create_form_submit`` parses, with the host
    name and color already resolved. ``account_id`` is deliberately absent: the
    resume step attaches the account the user just signed into.
    """

    git_url: str
    resolved_host_name: str
    branch: str
    launch_mode: LaunchMode
    ai_provider: AIProvider
    anthropic_api_key: str
    color: str
    backup_provider: BackupProvider
    backup_encryption_method: BackupEncryptionMethod
    backup_master_password: str
    is_save_backup_password: bool
    backup_api_key_env: str
    submitted_region: str
