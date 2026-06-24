"""Frontend (web-UI / browser) Sentry configuration for the desktop client.

The Python backend reports errors to Sentry via ``setup_sentry`` in
:mod:`imbue.minds.utils.sentry.core`. The browser-side web UI served by the
backend (the JinjaX pages under ``desktop_client/templates`` rendered through
``Base.jinja``) reports its own JavaScript errors to Sentry too, using the
vendored ``@sentry/browser`` bundle (``static/sentry.browser.min.js``) booted
by ``static/sentry_init.js``.

Backend (Python) and frontend (JavaScript) report to **separate** Sentry
projects, hence separate DSNs. A single Sentry project is tied to one platform
(its issue grouping, source-map handling, and release-health UI are all
platform-specific), so mixing a Python SDK and a JavaScript SDK into one project
is discouraged by Sentry even though the ingest endpoint technically accepts
both. We therefore keep one set of frontend DSNs (production / staging / dev)
mirroring the backend's three environments.

The DSN values below are PLACEHOLDERS -- create the three JavaScript Sentry
projects and paste their real DSNs here (replacing the ``__REPLACE_ME__``
markers). Until then, a misconfigured DSN simply means the browser SDK fails to
initialize; it never breaks the page (see ``sentry_init.js``).

Both the opt-in switch (``MINDS_SENTRY_ENABLED``) and the environment selection
(activated minds env -> production / staging / development) are shared with the
backend via :mod:`imbue.minds.utils.sentry.core`, so enabling Sentry lights up
the backend and the frontend together, under the same environment.
"""

from collections.abc import Mapping

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.build_info import resolve_git_sha
from imbue.minds.build_info import resolve_release_id
from imbue.minds.utils.sentry.core import SentryDeployEnvironment
from imbue.minds.utils.sentry.core import fixup_release_id
from imbue.minds.utils.sentry.core import is_sentry_enabled
from imbue.minds.utils.sentry.core import resolve_sentry_environment

# PLACEHOLDER frontend (JavaScript) Sentry DSNs -- one project per environment,
# distinct from the backend Python projects. Replace each ``__REPLACE_ME__``
# with the real DSN once the JavaScript projects exist in Sentry.
SENTRY_FRONTEND_DSN_PRODUCTION = "https://__REPLACE_ME__@o4504335315501056.ingest.us.sentry.io/__REPLACE_ME__"
SENTRY_FRONTEND_DSN_STAGING = "https://__REPLACE_ME__@o4504335315501056.ingest.us.sentry.io/__REPLACE_ME__"
SENTRY_FRONTEND_DSN_DEV = "https://__REPLACE_ME__@o4504335315501056.ingest.us.sentry.io/__REPLACE_ME__"

_FRONTEND_DSN_BY_ENVIRONMENT: Mapping[SentryDeployEnvironment, str] = {
    SentryDeployEnvironment.PRODUCTION: SENTRY_FRONTEND_DSN_PRODUCTION,
    SentryDeployEnvironment.STAGING: SENTRY_FRONTEND_DSN_STAGING,
    SentryDeployEnvironment.DEVELOPMENT: SENTRY_FRONTEND_DSN_DEV,
}

# Substring that marks a DSN as still being a placeholder; such DSNs are never
# handed to the browser, so an unconfigured project silently disables frontend
# reporting instead of pointing the SDK at a bogus endpoint.
_PLACEHOLDER_DSN_MARKER = "__REPLACE_ME__"


class FrontendSentryConfig(FrozenModel):
    """The frontend Sentry settings the backend injects into each web-UI page.

    ``is_enabled`` mirrors the backend opt-in (``MINDS_SENTRY_ENABLED``);
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


def resolve_frontend_sentry_config() -> FrontendSentryConfig:
    """Resolve the frontend Sentry config from the current process environment.

    Reuses the backend's opt-in switch and environment selection so the web UI
    reports to Sentry exactly when (and under the same environment as) the
    Python backend. The release id + git sha come from the same Electron-passed
    env vars the backend uses.
    """
    environment = resolve_sentry_environment()
    dsn = _FRONTEND_DSN_BY_ENVIRONMENT[environment]
    if _PLACEHOLDER_DSN_MARKER in dsn:
        dsn = None
    return FrontendSentryConfig(
        is_enabled=is_sentry_enabled(),
        dsn=dsn,
        environment=environment.value,
        release=fixup_release_id(resolve_release_id()),
        git_sha=resolve_git_sha(),
    )


def frontend_sentry_browser_payload() -> dict[str, str] | None:
    """Browser-ready Sentry payload for the current process, or ``None`` if off.

    This is the single entry point the JinjaX ``Base`` layout calls (exposed as
    a Catalog global) to decide whether -- and with what config -- to emit the
    Sentry bootstrap on every page.
    """
    return resolve_frontend_sentry_config().to_browser_payload()
