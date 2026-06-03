# Plan: migrate `apps/minds` desktop_client templates from Jinja macros to JinjaX

## Overview

- The desktop client's `templates/` directory uses Jinja's macro + `{% extends %}` + `{% include %}` system. Macros are imported via `{% import '_macros.html' as ui %}` and called as `{{ ui.btn_link(...) }}` or via `{% call %}`. Layouts compose with `{% extends 'base.html' %}` + `{% block %}` overrides. The mechanics work but read as Jinja boilerplate, not as UI primitives.
- JinjaX (built on top of Jinja2) replaces that with HTML-like component tags: `<Button href="/foo" variant="primary">Create</Button>`. Each component is its own `.jinja` file with a declared prop signature at the top. Slots replace `{% call %}`. Subfolder namespacing replaces ad-hoc import paths.
- The migration is atomic — one PR flips every consumer at once. There is no transition period with both engines wired up in parallel.
- JinjaX is a thin layer on Jinja2: the `Catalog` owns a Jinja `Environment` (`catalog.jinja_env`) we can configure exactly like the current `JINJA_ENV`. Autoescape, `tojson`, custom filters, and `FileSystemLoader` all keep working.
- Scope is limited to `apps/minds/imbue/minds/desktop_client/templates/`. `libs/mngr_forward`'s templates have no macros and stay on plain Jinja2.
- **Pages are components too.** During Phase 1 implementation we discovered that JinjaX's `<Component>` tag resolution only works when you call `catalog.render("Name")` — rendering a snake_case `.html` template through `catalog.jinja_env.get_template(...)` directly bypasses JinjaX's runtime setup (loader-priming, `__prefix` context var). To stay idiomatic, page templates become PascalCase `.jinja` components too, but live in a dedicated `templates/pages/` subdirectory so the file tree still visually distinguishes them from primitive components at `templates/`. Auth pages stay under `templates/auth/` alongside auth components.

## Expected behavior

- Rendered HTML stays semantically equivalent for end users. Pixel-equivalence is not guaranteed (whitespace and attribute ordering may shift), but every visible feature behaves identically.
- The public Python API in `templates.py` is unchanged: `render_landing_page(...)`, `render_create_form(...)`, etc. keep their signatures and docstrings. Callers (route handlers, tests) need zero changes.
- For developers writing templates:
  - `<Button variant="primary" href="/create">Create</Button>` replaces `{{ ui.btn_link('Create', '/create', variant='primary') }}`.
  - `<Card>...body...</Card>` replaces `{% call ui.card() %}...body...{% endcall %}`.
  - `<Base title="Projects">...page content...</Base>` replaces `{% extends 'base.html' %}` + `{% block title %}` + `{% block content %}`.
  - `<auth.AuthBase>...</auth.AuthBase>` (subfolder namespacing) replaces `{% extends 'auth/_auth_base.html' %}` + `{% block card_content %}`.
  - Page templates remain snake_case `.html` files at their current paths; components are PascalCase `.jinja` files alongside them.
- Permission subpages stop extending a monolithic base and instead compose: `<PermissionsDialog><PermissionsHeader.../><PermissionsForm>form body</PermissionsForm><PermissionsManualCredentials/><PermissionsError/></PermissionsDialog>`.
- The styleguide page (`/_dev/styleguide`) gains a swatch per JinjaX component (CardRow, PageContainer, Opt, auth.OauthIcon) on top of the existing Button/Notice/Spinner/TextInput examples. Existing `data-token`/`:root` ratchet check still passes.
- No change to: `static/*.css`, `static/*.js`, FastAPI routing, the dev styleguide's CSS-token coverage, or the recovery page's inline-HTML approach in Python.

## Implementation plan

### New dependency

- `apps/minds/pyproject.toml` — add `"jinjax>=0.45"` to `[project.dependencies]`. Run `uv sync --all-packages`.

### Catalog setup in `apps/minds/imbue/minds/desktop_client/templates.py`

- Replace the `from jinja2 import Environment, FileSystemLoader, select_autoescape` block and the `JINJA_ENV` constant with:
  - `from jinjax import Catalog`
  - `CATALOG: Final[Catalog] = Catalog()` plus `CATALOG.add_folder(str(TEMPLATE_DIR))` at module scope.
- JinjaX subfolder namespacing kicks in automatically: a file at `templates/auth/AuthBase.jinja` becomes `<auth.AuthBase>` in component context.
- For each `render_*` function: swap `JINJA_ENV.get_template("foo.html").render(...)` for `CATALOG.jinja_env.get_template("foo.html").render(...)`. The `Catalog` env has the JinjaX extension registered, so pages can use `<Component>` tags inside the `.html` files. No other body changes.
- `templates.py` stays a single file. ~1000-line shape preserved; one line changes per render function. The recovery-page inline HTML/CSS/JS (`_RECOVERY_STYLE`, `_RECOVERY_SCRIPT`, `render_recovery_page`) is **not** converted in this PR.

### New JinjaX components (top-level `templates/`)

Each file starts with `{# def args... #}` (JinjaX prop signature) followed by a short usage docstring comment. Tailwind variant maps stay inline via `{% set variants = {...} %}` at the top of the file.

Layout / containers:

- `templates/Base.jinja` — full HTML shell. Props: `title="Minds"`, `body_class="bg-zinc-50 text-zinc-900 font-sans antialiased"`, `body_style=""`, `head=""`, `scripts=""`. Default content slot for page body. Replaces `base.html`.
- `templates/PageContainer.jinja` — `max-w-[720px] mx-auto px-6 py-12` wrapper. Replaces `page_container()` macro.

Buttons (mirrors three macros — keep distinct components to preserve current ergonomics):

- `templates/Button.jinja` — `<button type="button">`. Props: `variant="secondary"`, `extra=""`, `id=""`, `onclick=""`, `block=false`. Default slot for label.
- `templates/ButtonLink.jinja` — `<a href="...">`. Props: `href`, `variant="secondary"`, `extra=""`, `id=""`, `block=false`. Default slot for label.
- `templates/ButtonSubmit.jinja` — `<button type="submit">`. Props: `variant="primary"`, `extra=""`, `id=""`, `form=""`, `block=false`. Default slot for label.

(The shared `_BTN_BASE` class string + `_BTN_VARIANTS` map is duplicated inline in each of the three files. See Open Questions.)

Surfaces:

- `templates/Card.jinja` — basic card wrapper. Props: `extra=""`. Default slot for body. Replaces `card()`.
- `templates/CardRow.jinja` — card with flex row layout. Props: `extra=""`. Default slot. Replaces `card_row()`.
- `templates/Notice.jinja` — info/warn/success/error notice box. Props: `variant="info"`, `extra=""`. Inline `_NOTICE_VARIANTS` map. Default slot.
- `templates/Spinner.jinja` — three-size spinner. Props: `size="md"`, `extra=""`. Inline `dim` map.
- `templates/TextInput.jinja` — styled text input. Props: `name`, `value=""`, `placeholder=""`, `type="text"`, `id=""`, `required=false`, `extra=""`.
- `templates/Opt.jinja` — onboarding option card. Props: `val`, `title`, `desc`, `editable=false`, `selected=false`, `preset=""`, `placeholder=""`, `rows=2`.

Partials:

- `templates/Associate.jinja` — workspace-association notice/form. Props: `agent_id`, `accounts`, `redirect_url=""`. Replaces `_associate.html` (currently `{% include %}`'d by `sharing.html` and `workspace_settings.html`).

Permissions (5 components — decomposes the monolithic `permissions.html` base):

- `templates/PermissionsDialog.jinja` — modal backdrop, dialog card, close button, and the ~80 lines of submit/deny/escape JS that lived in the `scripts` block. Props: `agent_id`, `accent`. Default content slot for everything inside the dialog. Captures the JS, `body_class="bg-transparent ..."`, and `body_style="--workspace-accent: ..."` concerns. Internally wraps `<Base>`.
- `templates/PermissionsHeader.jinja` — `<h1>` plus the rationale card. Props: `display_name`, `ws_name`, `agent_id`, `rationale`, plus optional `header_display_name_html=""` and `rationale_label_html=""` to support the existing `header_display_name` and `rationale_label` override blocks. Defaults to plain text.
- `templates/PermissionsForm.jinja` — `<form>` element with `action="/requests/{request_id}/grant"` plus the Deny/Approve buttons. Props: `request_id`. Default slot for form body (checkboxes, etc.). The progress notice is **not** part of this component — subpages render it inline next to the form using `<Notice variant="info">`, matching the current sibling-of-form positioning.
- `templates/PermissionsManualCredentials.jinja` — the hidden manual-credentials info box. No props. Static markup (populated by JS).
- `templates/PermissionsError.jinja` — the hidden error-message box. No props. Static markup (populated by JS).

### New JinjaX components (`templates/auth/`)

- `templates/auth/AuthBase.jinja` — auth-card layout variant. Props: `title`, `card_extra="max-w-[420px]"`. Internally wraps `<Base>` with the auth-specific `body_class` and content wrapped in the card div. Default slot for card content. Replaces `auth/_auth_base.html`.
- `templates/auth/OauthIcon.jinja` — single component, `provider` prop selects between Google and GitHub SVG paths. Props: `provider` (`"google"` or `"github"`). Replaces `_oauth_icons.html`'s two macros.

### Pages to rewrite (drop `{% extends %}` / `{% block %}` / `{% import %}` / `{% call %}` / `{% include %}`)

Each page becomes a `.html` Jinja template that uses `<Component>` tags inside (the JinjaX extension on `catalog.jinja_env` makes this work). Pattern: open with `<Base title="...">`, body inside, close with `</Base>`. Scripts go in `<Base scripts="...">` slot or — for multi-line scripts — `{% set scripts %}<script>...</script>{% endset %}` followed by `<Base scripts={{ scripts }}>`.

- `templates/landing.html`
- `templates/welcome.html`
- `templates/create.html`
- `templates/creating.html`
- `templates/destroying.html`
- `templates/accounts.html`
- `templates/workspace_settings.html`
- `templates/sharing.html`
- `templates/dev_styleguide.html` (+ new component examples per below)
- `templates/chrome.html` (no macros today, but uses `{% extends %}`)
- `templates/sidebar.html` (same)
- `templates/login.html`
- `templates/login_redirect.html`
- `templates/auth_error.html`
- `templates/request_unavailable.html`
- `templates/latchkey_predefined_permission.html` — rewrite to compose `<PermissionsDialog>`/`<PermissionsHeader>`/`<PermissionsForm>` etc. (drops `{% extends 'permissions.html' %}`).
- `templates/latchkey_file_sharing_permission.html` — same.
- `templates/auth/signup_signin.html` — uses `<auth.AuthBase>` + `<auth.OauthIcon provider="google"/>`.
- `templates/auth/check_email.html` — `<auth.AuthBase>`.
- `templates/auth/forgot_password.html` — `<auth.AuthBase>`.
- `templates/auth/settings.html` — `<auth.AuthBase>`.
- `templates/auth/oauth_close.html` — `<auth.AuthBase>`.

### Styleguide additions in `templates/dev_styleguide.html`

Beyond converting existing examples to the new components, add swatches for components that aren't currently demonstrated:

- `<CardRow>` example.
- `<PageContainer>` boundary indicator.
- `<Opt>` editable / non-editable / selected examples.
- `<auth.OauthIcon provider="google"/>` and `<auth.OauthIcon provider="github"/>`.

### Files to delete

- `templates/_macros.html`
- `templates/base.html`
- `templates/_associate.html`
- `templates/permissions.html` — fully replaced by the five `Permissions*` components; subpages compose directly.
- `templates/auth/_auth_base.html`
- `templates/auth/_oauth_icons.html`

### Wheel packaging

- `apps/minds/pyproject.toml` ships `.html` files inside the `imbue` package via hatchling's default behavior (`[tool.hatch.build.targets.wheel] packages = ["imbue"]`). `.jinja` files inside the same directory follow the same default-inclusion rule.
- Verified during implementation: adding a redundant `force-include` for `imbue/minds/desktop_client/templates` causes hatchling to write every template *twice* into the wheel (zipfile-duplicate warnings). So we deliberately do **not** add a force-include block.
- Smoke-verify the built wheel includes `.jinja` files: `uv build apps/minds && unzip -l dist/minds-*.whl | grep '\.jinja'` after the migration is complete.

### Tests

- `apps/minds/imbue/minds/desktop_client/templates_test.py`:
  - Run the existing substring-asserting tests as-is. Fix any that break due to whitespace / attribute-ordering differences. Expected to mostly pass.
  - Add a small set of new unit tests:
    - `test_button_renders_primary_variant_classes` — assert each `_BTN_VARIANTS` class set appears for the variant.
    - `test_button_link_renders_anchor_with_href` — `<a href="...">`.
    - `test_button_submit_has_form_attribute_when_passed`.
    - `test_notice_renders_each_variant`.
    - `test_oauth_icon_google_includes_google_path` / `test_oauth_icon_github_includes_github_path` — assert the right SVG path string is in the rendered HTML.
    - `test_card_default_slot_renders_body`.
  - Existing `templates_test.py` ratchet (tokens-vs-swatches cross-check) is unaffected because `data-token` attributes and `:root` CSS are not in scope.
- No new ratchets in `test_ratchets.py`.

## Implementation phases

Each phase leaves the working tree in a state where `just test-quick apps/minds` passes. The whole migration ships as one PR; phases are about the agent's execution order, not separate commits.

### Phase 1 — dependency + Catalog scaffolding

- Add `jinjax>=0.45` to `apps/minds/pyproject.toml`. Run `uv sync --all-packages`.
- In `templates.py`, **add** the `CATALOG` constant alongside the existing `JINJA_ENV` (do not remove the latter yet). This keeps every existing page rendering through Jinja while the catalog is being populated.
- Verify `CATALOG.jinja_env` shares the same `FileSystemLoader` shape and autoescape behavior. No render functions change yet.
- Outcome: all tests still pass. JinjaX is now imported but no consumer uses it.

### Phase 2 — write all component files

- Create the 17 top-level `.jinja` components in `templates/` and the 2 components in `templates/auth/`.
- Verify they render in isolation: a temporary scratch test renders each component standalone with representative props.
- Outcome: components exist on disk but no page references them yet. Pages still render through `JINJA_ENV` against the old `_macros.html` etc. Tests still pass.

### Phase 3 — migrate non-permission pages

- Switch the render functions for non-permission pages to `CATALOG.jinja_env.get_template(...).render(...)`.
- Rewrite each non-permission page to drop `{% extends %}` / `{% block %}` / `{% import %}` / `{% include %}` / `{% call %}` and use the new component tags instead.
- Tests should pass page-by-page. Iterate on each page until its substring assertions hold.

### Phase 4 — migrate permissions pages

- Switch `latchkey_predefined_permission.html` and `latchkey_file_sharing_permission.html` to compose the five `Permissions*` components.
- Switch their render functions (`render_latchkey_predefined_permission_dialog` etc. — preserve exact signatures) to the catalog.
- Permission tests pass.

### Phase 5 — delete obsolete files + drop `JINJA_ENV`

- Delete `_macros.html`, `base.html`, `_associate.html`, `permissions.html`, `auth/_auth_base.html`, `auth/_oauth_icons.html`.
- Remove the `JINJA_ENV` constant and the obsolete `from jinja2 import Environment, ...` imports from `templates.py`.
- Run the full test suite. Fix any lingering breakage.

### Phase 6 — styleguide expansion + new component tests

- Extend `dev_styleguide.html` with CardRow / PageContainer / Opt / OauthIcon examples.
- Add the new component-level unit tests in `templates_test.py`.
- Run `just test-quick apps/minds` and ensure new + existing tests pass.
- Confirm wheel packaging: `uv build apps/minds && unzip -l ...` lists all new `.jinja` files.

### Phase 7 — final verification

- `just test-offload` for the full suite across all projects.
- `/autofix` to verify and fix code-quality issues.
- `/verify-conversation` for behavioral review.
- Manual UI smoke: launch the desktop client, visit landing / create / welcome / sharing / workspace settings / `/_dev/styleguide` / a permission dialog. Confirm visible parity.

## Testing strategy

### Unit tests

- Existing substring assertions in `templates_test.py` survive. They assert on rendered strings (e.g. `"forever-claude-template" in html`, `"window.location.href" in html`), which JinjaX preserves.
- New component-level tests render individual components in isolation via `CATALOG.render("Button", variant="primary", ...)` and assert the right Tailwind class set is in the output. One small test per component primitive (Button family, Card, Notice, Spinner, TextInput, Opt, Associate, OauthIcon). Permission-decomposition components also get one shape test each.

### Integration tests

- `test_desktop_client.py` exercises the FastAPI routes end-to-end through the FastAPI TestClient. These will catch any regression where a route now produces broken HTML (e.g. unclosed tag, missing component).

### Ratchet

- `templates_test.py`'s existing ratchet that cross-checks `data-token` swatches in `dev_styleguide.html` against `:root` declarations in `static/tokens.css` keeps working unchanged. The styleguide rewrite preserves every swatch.

### Edge cases to verify

- Autoescape: a render call that passes user-controlled strings (e.g. `error_message` in `render_create_form`, `message` in `render_auth_error_page`) should escape HTML the same way it does today. Add an inline assertion in the relevant test that `<script>` injected via a kwarg is HTML-escaped.
- `tojson` filter: `sharing.html` uses `{{ initial_emails | tojson }}` in a JSON island. Verify the catalog's Jinja env has the same default filter set (it does — JinjaX wraps a stock Jinja env).
- Empty-string defaults: macros today accept `extra=''` and conditionally render attributes only when truthy. JinjaX component defaults should match exactly to avoid attribute drift (e.g. `id=""` not rendered when empty).
- Subfolder namespacing: confirm `<auth.OauthIcon provider="github"/>` resolves to `templates/auth/OauthIcon.jinja` and not to a top-level fallback.
- Wheel: `uv build` produces a wheel that includes all `.jinja` files. The smoke-check command lives in Phase 6.

### Manual UI verification

- `just minds-start` and exercise the golden paths above. Specifically inspect:
  - Landing page with multiple workspaces (`<Base>`, `<ButtonLink>`, `<PageContainer>`, accent stripes).
  - Welcome page modal (`<Base>`, `<ButtonButton>`, `<ButtonLink>`).
  - Sharing editor (`<Base>`, `<Spinner>`, `<Card>`, `<Associate>`).
  - Permission dialog with checkboxes (full `Permissions*` composition + extra scripts).
  - Auth signup tab and OAuth icons (`<auth.AuthBase>`, `<auth.OauthIcon>`).
  - `/_dev/styleguide` — every swatch present, including new ones.

## Open questions

- **Button decomposition.** Three components (`Button`, `ButtonLink`, `ButtonSubmit`) was chosen to mirror the three macros 1:1, but it duplicates the `_BTN_VARIANTS` map across three files. Alternative: one `<Button>` with `kind="button|link|submit"` prop and a conditional render of `<a>` vs `<button type=...>`. Would consolidate the variant map but departs from "mirror macros 1:1". Pick one before Phase 2.
- **Variant maps inline vs lifted.** Today `_BTN_VARIANTS` and `_NOTICE_VARIANTS` sit at the top of `_macros.html`. With three button components inlining the same map, we get 3x duplication. If we want one source of truth, options are: (a) a tiny shared `_variants.jinja` partial that each button component `{% include %}`s (works but awkward); (b) move maps to Python and pass via catalog globals (clean, but couples Python and template variant lists). Default in this plan is inline duplication; flag for confirmation.
- **Recovery page.** `render_recovery_page` builds its HTML by concatenating inline strings in `templates.py` rather than via a Jinja template. It's untouched by this migration. Worth extracting to a `RecoveryPage.jinja` (and `RecoveryStyle.jinja` / `RecoveryScript.jinja`) component in a follow-up PR? Out of scope here, but flag as a future opportunity.
- **`render_destroying_page` / `render_chrome_page` / `render_sidebar_page` template internals.** These pages don't currently use `_macros.html` but do use `{% extends 'base.html' %}` + `{% block %}`. Rewriting them is straightforward but worth listing explicitly so nothing is missed. Done in this plan.
- **JinjaX caching behavior.** In production we'd want `auto_reload=False` for performance; in dev the auto-reload is convenient. `Catalog()` defaults — confirm what they are at the chosen version (0.45+) and whether we need to gate by an env var. Default in this plan is "use catalog defaults"; verify before merging.
- **Static-asset path prefix.** Components reference `/_static/...` paths. JinjaX has no opinion on assets; the strings carry over unchanged. Flag in case anything in the JinjaX setup conflicts (it shouldn't).
- **The `accent` variable.** Several pages set `--workspace-accent` in `<body style="...">`. With `<Base body_style="...">`, the per-page accent is passed as a prop. Confirm the prop interpolation works for the `style` attr value (it should; JinjaX uses Jinja interpolation for attributes).
