Large pass over the desktop client's HTML templates to extract recurring inline Tailwind patterns into JinjaX primitives. The change set is mostly internal -- rendered behavior is preserved -- but a few visual tweaks ride along.

New / generalized primitives (under ``apps/minds/imbue/minds/desktop_client/templates/``):

- ``Card`` (rewritten): ``layout`` (``block`` / ``row`` / ``row-spread``), ``padding`` (``default`` / ``tight``), ``interactive``, ``tag`` (``div`` / ``a`` / ``button``), ``href``, plus JinjaX ``attrs`` passthrough for arbitrary HTML attributes. The visual shell moves into a shared ``.minds-card`` CSS class in ``tokens.css`` so JS-rendered surfaces (the Landing providers panel) reference one source of truth.
- ``CardPage`` (renamed from ``auth/AuthBase``): centered-card layout used by the auth flow + the Create workspace form. ``padding="default"`` (``p-10``, auth) or ``"form"`` (``p-6``, Create); ``max_width`` is a Tailwind utility. The Login / AuthError pages now go through this primitive instead of hand-rolling the centered card.
- ``Button`` / ``ButtonLink`` / ``ButtonSubmit``: add a ``size`` axis (``md`` default, ``lg`` for prominent block CTAs, ``icon`` for square padding). Disabled buttons fade to ``opacity-30`` (was ``opacity-50``). All three now use JinjaX ``attrs.render()`` passthrough.
- ``TitlebarButton``: new primitive for the dark title-bar window controls. ``variant="nav"`` (left-side icons) / ``"control"`` (min/max/close); ``tone="default"`` / ``"danger"`` (close button's red hover).
- ``Link``: new primitive for inline ``text-blue-600 hover:underline`` anchors. ``weight="regular"`` (default) or ``"medium"`` for the auth-flow tab-switch / back-link affordances.
- ``Select`` / ``Textarea``: new primitives sharing TextInput's focus-ring token via a new ``INPUT_BASE`` catalog global.
- ``FormLabel``: new primitive for form-field labels. ``inline=False`` (block, mb-1.5) or ``inline=True`` (sits beside its control). Prop is ``target=`` (the HTML ``for`` attribute id).
- ``Icon24`` / ``Icon12``: new primitives wrapping the 24x24 lucide stroke icons + the 12x12 title-bar chrome glyphs. Path data lives in ``ICONS_24`` / ``ICONS_12`` dicts in ``templates.py``.
- ``Notice``: drops the bespoke ``extra`` prop in favor of attrs passthrough so callers can pass ``id=``, ``class="hidden"``, ``data-*`` alongside ``variant=``.
- ``auth.OauthButton``: new primitive composing ``auth.OauthIcon`` + the brand label, picked by ``provider="google"|"github"``.
- ``Spinner``: gains ``tone="accent"`` (blue ring) for primary-action spinners; old inline ``border-blue-300 border-t-blue-600 animate-spin`` patterns migrate to ``<Spinner tone="accent">``.

Standardization sweeps:

- **Text colors**: banished ``text-zinc-600`` and ``text-zinc-100`` so each remaining shade carries one role (``zinc-900`` primary, ``zinc-700`` body, ``zinc-500`` secondary/label, ``zinc-400`` muted, ``zinc-200`` on-dark). Section labels (SectionHeader, inline ``<h2>`` labels) lift from 600 to 500; body paragraphs lift from 600 to 700; ghost button text moves from 600 to 700.
- **Corner radii**: retired bare ``rounded`` (20 sites swept to explicit ``rounded-md``) and ``rounded-2xl`` (PermissionsDialog + RequestUnavailable fold to ``rounded-xl`` so dialog chrome matches card chrome).
- **Borders**: 2 accidental ``border-zinc-300`` sites fold to canonical ``border-zinc-200``.
- **Shadows**: ``.minds-card`` baseline has no shadow; the ``interactive`` Card flag adds ``hover:shadow-sm``. Non-clickable cards (PermissionsHeader, the Latchkey permission cards, Associate) read as flat surfaces.
- **StatusBadge**: the ``warn`` variant drops its one-off border so all five variants share a uniform pill treatment.

CSS classes anchor a few JS-rendered surfaces that can't call JinjaX: ``.minds-card`` (Card shell), ``.spinner`` / ``.spinner-accent`` (Spinner), ``.code-pill`` (inline mono pill in Sharing).

A new ``apps/minds/imbue/minds/desktop_client/templates/README.md`` documents the rule ("use a primitive before reaching for inline Tailwind"), the catalog, where the shared tokens live, the visual-diff workflow, and the JinjaX gotchas the branch shook out (Python-keyword props, nested ``{# #}`` comments, literal ``<Tag>`` in docstrings, ``:attr="..."`` for component-tag dynamic attributes, ``!important`` on the ghost-Button link-style recipe).

``apps/minds/scripts/visual_diff.py``: the screenshot step now waits for Tailwind to inject its generated stylesheet before snapping (was a flat 400ms timeout that produced unstyled screenshots on slow machines or when ``tailwind.js`` was missing). The compare report's per-scenario thumbnails open a click-through lightbox: click image swaps A/B, ``←``/``→`` step between differing scenarios, ``Esc`` closes.

Visible end-user impact is small and is mostly subtle visual polish: the auth-flow CTAs gain canonical ``p-10`` padding (~2-4px shifts), the Landing project-row icon buttons darken slightly under the ghost variant, the auth pages' "Sign in"/"Back to" links pick up consistent ``font-medium`` styling, and a couple of misaligned form-control padding pairs now line up vertically. The ``Configure...`` disclosure on the Create form correctly renders at ``text-xs font-normal`` after a follow-up to add ``!important`` to the link-style recipe overrides.
