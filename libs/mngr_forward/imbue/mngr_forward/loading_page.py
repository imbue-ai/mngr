"""The canonical "Loading workspace" page.

A single source of truth for the loading screen so the mngr_forward proxy
loader and any downstream consumer's loading/recovery page render the *same*
HTML in their loading state -- rather than two hand-matched markups that drift
apart.

The proxy serves it with a small background poll that re-attempts the workspace
until it answers; a consumer can reuse it and layer its own controls and script
on top via the ``card_extra`` / ``style_extra`` / ``body_extra`` hooks.
"""

from typing import Final

# The shared stylesheet, split so a consumer that renders the card inside its own
# shell (e.g. the minds recovery page under the desktop titlebar) can pull in only
# the card-scoped rules (``LOADING_PAGE_CARD_CSS``) and supply its own body layout,
# without the full-viewport ``body`` centering fighting a fixed titlebar. The proxy
# loader uses ``LOADING_PAGE_CSS`` (both halves) exactly as before.
#
# ``_LOADING_PAGE_BODY_CSS`` centers a single ``.card`` in the viewport.
_LOADING_PAGE_BODY_CSS: Final[str] = """\
      html, body { height: 100%; margin: 0; }
      body {
        background: #fafafa;
        color: #18181b;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 24px;
        box-sizing: border-box;
      }
"""

# ``LOADING_PAGE_CARD_CSS`` styles the card itself; ``.row`` lays the spinner
# beside the heading/message block. Safe to inject into any host document's head.
LOADING_PAGE_CARD_CSS: Final[str] = """\
      .card {
        background: #fff;
        border: 1px solid #e4e4e7;
        border-radius: 12px;
        padding: 24px;
        max-width: 480px;
        width: 100%;
        box-sizing: border-box;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
      }
      .row { display: flex; align-items: center; gap: 12px; }
      h1 { font-size: 1.125rem; font-weight: 600; margin: 0; color: #18181b; }
      p { margin: 6px 0 0; color: #52525b; font-size: 0.875rem; line-height: 1.5; }
      /* With no message (the loading state) the empty <p> is removed so the
         heading stays vertically centered against the spinner. */
      p:empty { display: none; }
      .spinner {
        width: 20px;
        height: 20px;
        border: 2px solid #e4e4e7;
        border-top-color: #18181b;
        border-radius: 50%;
        animation: spin 1s linear infinite;
        flex-shrink: 0;
      }
      @keyframes spin { to { transform: rotate(360deg); } }
"""

LOADING_PAGE_CSS: Final[str] = _LOADING_PAGE_BODY_CSS + LOADING_PAGE_CARD_CSS

# The default heading/message. A consumer may override these at runtime (via
# its own script) for non-loading states, but the initial render -- and the
# proxy loader always -- shows this. The loading state has no message; the
# empty <p> stays in the markup so a consumer's script can populate it for the
# other states.
_LOADING_TITLE: Final[str] = "Loading workspace"
_LOADING_MESSAGE: Final[str] = ""


def render_loading_card(*, card_attrs: str = "", card_extra: str = "") -> str:
    """Render just the loading ``.card`` fragment (no document wrapper).

    Used by ``render_loading_page`` for the full proxy-loader document, and by
    consumers that place the card inside their own shell (e.g. the minds recovery
    page under the desktop titlebar). Pair it with ``LOADING_PAGE_CARD_CSS`` in the
    host document's head.

    ``card_attrs``   -- extra attributes on the ``.card`` element (e.g. ``data-*``).
    ``card_extra``   -- extra markup appended inside the ``.card`` (e.g. buttons).
    """
    return f"""    <div class="card"{card_attrs}>
      <div class="row">
        <div id="loading-spinner" class="spinner" aria-hidden="true"></div>
        <div>
          <h1 id="loading-title">{_LOADING_TITLE}</h1>
          <p id="loading-message">{_LOADING_MESSAGE}</p>
        </div>
      </div>
{card_extra}    </div>
"""


def render_loading_page(
    *,
    head_extra: str = "",
    style_extra: str = "",
    card_attrs: str = "",
    card_extra: str = "",
    body_extra: str = "",
) -> str:
    """Render the canonical "Loading workspace" page.

    ``head_extra``   -- extra markup inside ``<head>`` (e.g. a ``<meta>`` or ``<link>`` tag).
    ``style_extra``  -- extra CSS appended to ``LOADING_PAGE_CSS``.
    ``card_attrs``   -- extra attributes on the ``.card`` element (e.g. ``data-*``).
    ``card_extra``   -- extra markup appended inside the ``.card`` (e.g. buttons).
    ``body_extra``   -- extra markup after the ``.card`` (e.g. a ``<script>``).

    With every hook empty this is exactly the proxy loader.
    """
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
{head_extra}    <title>{_LOADING_TITLE}</title>
    <style>
{LOADING_PAGE_CSS}{style_extra}    </style>
  </head>
  <body>
{render_loading_card(card_attrs=card_attrs, card_extra=card_extra)}{body_extra}  </body>
</html>
"""
