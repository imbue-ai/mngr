"""The pages the proxy serves into an agent origin when it answers a request itself.

A browser navigation the proxy cannot hand to a backend gets one of these
instead of bare text: the loading page, which re-attempts the origin (503), a
page saying the connection broke off (502/504), or a page saying the origin's
sign-in link is spent (403). Every 502/503/504 the proxy answers itself -- page or
plain text -- also carries two headers, so any client can tell why, and whether
the request ever reached the backend:

- ``X-Mngr-Forward-Reason``: a :class:`ProxyAnswerReason` value.
- ``X-Mngr-Forward-Request-Sent``: ``no`` when the request never left the proxy,
  ``yes`` when it was sent (or partly sent) to the backend, which may have acted
  on it.

The pages say nothing about why. A page framed by another window tells that
window its reason (see :data:`_PAGE_SCRIPT` and the README's "When the proxy
answers a request itself" section), and the window decides what to show.
"""

import html
import json
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr_forward.data_types import ProxyAnswerReason
from imbue.mngr_forward.loading_page import LOADING_PAGE_CSS
from imbue.mngr_forward.loading_page import render_loading_page

PROXY_REASON_HEADER: Final[str] = "X-Mngr-Forward-Reason"
REQUEST_SENT_HEADER: Final[str] = "X-Mngr-Forward-Request-Sent"
EXPIRED_LINK_PLAIN_TEXT: Final[str] = "Invalid or expired subdomain auth token"

_POLL_INTERVAL_MS: Final[int] = 1_000

_DROPPED_MESSAGE: Final[str] = "The connection to this workspace dropped."

_PAGE_CSS: Final[str] = """\
      .card .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
      .card button, .card a.button {
        font: inherit; font-size: 0.875rem; padding: 6px 12px; border-radius: 8px; cursor: pointer;
        border: 1px solid #18181b; background: #18181b; color: #fff;
        display: inline-block; text-decoration: none;
      }
      .card details { margin-top: 12px; font-size: 0.8125rem; color: #52525b; }
      .card details summary { cursor: pointer; }
      .card details pre { white-space: pre-wrap; word-break: break-word; margin: 6px 0 0; }
"""

# The page's behaviour, driven by the JSON config the server embeds.
#
# A framed page posts its reason to its parent window on load, again whenever
# the reason changes, and says it is going away on pagehide, so the parent can
# draw its own account of the page over the frame and take it down once the
# frame moves on. The parent is whatever the frame-ancestors policy every
# proxied origin carries allows: the agent's own origin family or an embedder
# the proxy was told to allow.
#
# A loading page polls its own URL: a 503 keeps it waiting (and its reason
# header is passed on), anything else reloads it into the answer. Polling rather
# than reloading itself on a timer matters: a full reload of a view steals OS
# focus from any sibling Electron view layered over it
# (https://github.com/electron/electron/issues/42578), which makes an overlay's
# text field impossible to type into.
_PAGE_SCRIPT: Final[str] = """\
    <script>
      (function () {
        var config = JSON.parse(document.getElementById('mngr-forward-page-config').textContent);
        var reason = config.reason;
        var isFramed = window.parent !== window;
        function announce() {
          if (!isFramed) return;
          window.parent.postMessage({
            type: 'mngr-forward:page',
            reason: reason,
            status: config.status,
            details: config.details
          }, '*');
        }
        announce();
        window.addEventListener('pagehide', function () {
          if (isFramed) window.parent.postMessage({ type: 'mngr-forward:page-closed' }, '*');
        });
        if (config.status !== 503) return;
        function poll() {
          fetch(window.location.href, { credentials: 'same-origin', redirect: 'manual', cache: 'no-store' })
            .then(function (resp) {
              if (resp.status === 503) {
                var next = resp.headers.get('X-Mngr-Forward-Reason');
                if (next && next !== reason) {
                  reason = next;
                  announce();
                }
                setTimeout(poll, config.pollIntervalMs);
              } else {
                window.location.reload();
              }
            }, function () {
              setTimeout(poll, config.pollIntervalMs);
            });
        }
        setTimeout(poll, config.pollIntervalMs);
      })();
    </script>
"""


def _json_for_script(value: object) -> str:
    """JSON safe to embed inside a ``<script>`` element (no ``</`` can close it)."""
    return json.dumps(value).replace("</", "<\\/")


def _config_and_script(reason: ProxyAnswerReason, status_code: int, details: str) -> str:
    config = {
        "reason": reason.value,
        "status": status_code,
        "details": details,
        "pollIntervalMs": _POLL_INTERVAL_MS,
    }
    return (
        f"""    <script type="application/json" id="mngr-forward-page-config">{_json_for_script(config)}</script>\n"""
        + _PAGE_SCRIPT
    )


@pure
def render_proxy_answer_page(
    reason: ProxyAnswerReason,
    status_code: int,
    details: str,
) -> str:
    """Render the page for one of the proxy's own answers.

    A 503 is the loading page, which re-attempts the origin in the background.
    A 502 or 504 says the connection dropped, with "Try again" and ``details``
    (the proxy's plain-text answer) collapsed behind "See details".
    """
    script = _config_and_script(reason, status_code, details)
    if status_code == 503:
        return render_loading_page(card_attrs=f' data-reason="{reason.value}"', body_extra=script)
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>{html.escape(_DROPPED_MESSAGE)}</title>
    <style>
{LOADING_PAGE_CSS}{_PAGE_CSS}    </style>
  </head>
  <body>
    <div class="card" data-reason="{reason.value}">
      <h1>{html.escape(_DROPPED_MESSAGE)}</h1>
      <div class="actions">
        <button type="button" onclick="window.location.reload()">Try again</button>
      </div>
      <details><summary>See details</summary><pre>{html.escape(details)}</pre></details>
    </div>
{script}  </body>
</html>
"""


_EXPIRED_LINK_MESSAGE: Final[str] = "This link has expired. Open the workspace again from your workspaces list."


@pure
def render_expired_link_page(next_path: str) -> str:
    """Render the page for a spent or expired sign-in link to an agent origin.

    "Open workspace" goes to the path the link was for; with the link spent,
    that request takes the ordinary unauthenticated route, which re-signs the
    browser in through the bare origin when it still has a session there.
    """
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Link expired</title>
    <style>
{LOADING_PAGE_CSS}{_PAGE_CSS}    </style>
  </head>
  <body>
    <div class="card">
      <h1>{html.escape(_EXPIRED_LINK_MESSAGE)}</h1>
      <div class="actions">
        <a class="button" href="{html.escape(next_path, quote=True)}">Open workspace</a>
      </div>
      <details><summary>See details</summary><pre>{html.escape(EXPIRED_LINK_PLAIN_TEXT)}</pre></details>
    </div>
  </body>
</html>
"""
