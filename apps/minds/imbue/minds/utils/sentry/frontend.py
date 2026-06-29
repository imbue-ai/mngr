"""Frontend (web-UI / browser) Sentry configuration for the desktop client.

The Python backend reports errors to Sentry via ``setup_sentry`` in
:mod:`imbue.minds.utils.sentry.core`. The browser-side web UI served by the
backend (the JinjaX pages under ``desktop_client/templates`` rendered through
``Base.jinja``) reports its own JavaScript errors to Sentry too, using the
vendored ``@sentry/browser`` bundle (``static/sentry.browser.min.js``) booted
by ``static/sentry_init.js``.

The Python backend reports to its own (Python) Sentry projects; all of minds'
**JavaScript** -- both this browser web UI and the Electron main process
(``electron/sentry.js``) -- reports to one shared set of **JavaScript** Sentry
projects (production / staging / dev). Backend and frontend stay on separate
projects because a single Sentry project is tied to one platform (its issue
grouping, source-map handling, and release-health UI are all platform-specific),
so mixing a Python SDK and a JavaScript SDK into one project is discouraged even
though the ingest endpoint technically accepts both. The browser and Electron
main process, however, are both JavaScript and happily share one project set.

The browser web UI reports automatic JavaScript errors, so it is gated by the
same per-machine user setting as the backend's automatic error reporting
(``report_unexpected_errors`` in :class:`MindsConfig`, surfaced via the
first-launch consent screen and account settings). The caller passes that
setting in as ``is_error_reporting_enabled``; the environment selection
(activated minds env -> production / staging / development) is shared with the
backend via :mod:`imbue.minds.utils.sentry.core`, so the backend and the
frontend report under the same environment, release, and ``git_sha`` tag.
"""

from collections.abc import Mapping

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.build_info import resolve_git_sha
from imbue.minds.build_info import resolve_release_id
from imbue.minds.utils.sentry.core import SentryDeployEnvironment
from imbue.minds.utils.sentry.core import fixup_release_id
from imbue.minds.utils.sentry.core import resolve_sentry_environment

# Keep these in sync with the Sentry projects declared in apps/minds/electron/sentry.js.
SENTRY_FRONTEND_DSN_PRODUCTION = (
    "https://70356438f3a945b8e58cb0a6f8773d0a@o4504335315501056.ingest.us.sentry.io/4511620037804032"
)
SENTRY_FRONTEND_DSN_STAGING = (
    "https://b8ce0a0ea4d38de2bda94e5ff6168572@o4504335315501056.ingest.us.sentry.io/4511620045144064"
)
SENTRY_FRONTEND_DSN_DEV = (
    "https://ddc0f18beba95166b72eacd9d4b48bf0@o4504335315501056.ingest.us.sentry.io/4511620043243520"
)

_FRONTEND_DSN_BY_ENVIRONMENT: Mapping[SentryDeployEnvironment, str] = {
    SentryDeployEnvironment.PRODUCTION: SENTRY_FRONTEND_DSN_PRODUCTION,
    SentryDeployEnvironment.STAGING: SENTRY_FRONTEND_DSN_STAGING,
    SentryDeployEnvironment.DEVELOPMENT: SENTRY_FRONTEND_DSN_DEV,
}


class FrontendSentryConfig(FrozenModel):
    """The frontend Sentry settings the backend injects into each web-UI page.

    ``is_enabled`` reflects the user's ``report_unexpected_errors`` setting;
    ``dsn`` is ``None`` when reporting is disabled or the environment's DSN is
    still a placeholder. ``environment`` / ``release`` / ``git_sha`` match the
    values the backend reports so frontend and backend events line up.
    """

    is_enabled: bool
    dsn: str | None
    environment: str
    release: str
    git_sha: str

    def to_browser_payload(self) -> dict[str, str] | None:
        """Return the JSON-safe payload for the browser SDK, or ``None`` if off.

        ``None`` means the page must emit no Sentry bootstrap at all (reporting
        disabled or no real DSN configured for this environment).
        """
        if not self.is_enabled or self.dsn is None:
            return None
        return {
            "dsn": self.dsn,
            "environment": self.environment,
            "release": self.release,
            "git_sha": self.git_sha,
        }


def resolve_frontend_sentry_config(is_error_reporting_enabled: bool) -> FrontendSentryConfig:
    """Resolve the frontend Sentry config for the current process.

    ``is_error_reporting_enabled`` is the user's ``report_unexpected_errors``
    setting, so the web UI reports to Sentry exactly when the user has opted in.
    The environment selection is shared with the backend so frontend and backend
    events line up; the release id + git sha come from the same Electron-passed
    env vars the backend uses.
    """
    environment = resolve_sentry_environment()
    dsn = _FRONTEND_DSN_BY_ENVIRONMENT[environment]
    return FrontendSentryConfig(
        is_enabled=is_error_reporting_enabled,
        dsn=dsn,
        environment=environment.value,
        release=fixup_release_id(resolve_release_id()),
        git_sha=resolve_git_sha(),
    )


def frontend_sentry_browser_payload(is_error_reporting_enabled: bool) -> dict[str, str] | None:
    """Browser-ready Sentry payload for the current process, or ``None`` if off.

    ``is_error_reporting_enabled`` is the user's ``report_unexpected_errors``
    setting. This is the entry point the JinjaX ``Base`` layout reaches (via the
    Catalog global registered in ``desktop_client/templates.py``, which supplies
    the live setting) to decide whether -- and with what config -- to emit the
    Sentry bootstrap on every page.
    """
    return resolve_frontend_sentry_config(is_error_reporting_enabled).to_browser_payload()
