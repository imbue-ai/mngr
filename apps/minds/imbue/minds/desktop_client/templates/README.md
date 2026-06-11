# UI components (templates/)

JinjaX components that render the desktop client's HTML. Every visible
surface in the app should compose from these primitives -- inline
Tailwind class strings drift, and a centralized primitive lets one edit
re-skin every call site.

## The rule

**Before adding inline Tailwind for a UI surface, check whether a
primitive already covers it.** If you can't find one, look at
`/_dev/styleguide` in a running app (or
`templates/pages/DevStyleguide.jinja`) for the live catalog. Only then
should you reach for raw `<div class="...">` markup -- and if you find
yourself doing that more than once for the same shape, that's a signal
to lift a new primitive into `templates/`.

## The catalog

Pages live under `templates/pages/`; reusable components sit at the
root of `templates/`. Auth-flow components live under `templates/auth/`.

### Surfaces

| Component | Role |
|---|---|
| `Base` | Universal HTML scaffold (html/head/body, Tailwind CDN, tokens.css). Every page wraps in this. |
| `PageContainer` | Centered `max-w-[720px]` body wrapper. Default for in-app settings-style pages (Landing, Accounts, WorkspaceSettings, Sharing, Destroying). |
| `CardPage` | Centered-card layout for auth flow + form pages. `padding="default"` (`p-10`, auth) or `"form"` (`p-6`, Create); `max_width` is a Tailwind utility. |
| `Card` | Card surface with `layout`/`padding`/`interactive`/`tag`/`href` props. Pulls `.minds-card` from `tokens.css` for the shared shell. |
| `Modal` | Overlay dialog with backdrop. Used for confirmation dialogs (Welcome's skip-account prompt, WorkspaceSettings' destroy modal). |
| `PermissionsHeader` / `PermissionsForm` / `PermissionsError` / `PermissionsManualCredentials` | Composable building blocks for the latchkey permission-request detail fragments (`pages.LatchkeyPredefinedPermission`, `pages.LatchkeyFileSharingPermission`). The surrounding modal chrome lives in the inbox shell (`pages.Inbox`), not in a separate dialog primitive. |

### Interactive

| Component | Role |
|---|---|
| `Button` / `ButtonLink` / `ButtonSubmit` | The three button forms (`<button type="button">`, `<a>`, `<button type="submit">`). Share `variant` (`primary` / `secondary` / `danger` / `success` / `ghost`) and `size` (`md` / `lg` / `icon`) via `BTN_BASE` / `BTN_SIZES` / `BTN_VARIANTS` in `templates.py`. |
| `TitlebarButton` | Window-control buttons for the title bar. `variant="nav"` (icon hits) / `"control"` (min/max/close); `tone="default"` / `"danger"` (close red hover). |
| `Link` | Inline blue-underline `<a>` link. `weight="regular"` (default) / `"medium"` (auth-flow tab-switch + back-link affordances). For click-toggle "buttons that look like links" (Configure / Adjust / Show advanced), use the ghost-Button recipe in the styleguide instead. |
| `auth.OauthButton` | White-card "Continue with Google / GitHub" button. Composes `auth.OauthIcon` + the brand label. |
| `DialogCloseButton` | Top-right X used by overlay dialogs. |

### Form controls

| Component | Role |
|---|---|
| `TextInput` | `<input>`. `radius="md"` (default) / `"lg"` (auth cards). Shares `INPUT_BASE` (border + focus ring) with Select/Textarea. |
| `Select` | `<select>`. Children are `<option>` elements. `width="w-full"` default; pass `w-48` for compact selects beside a label. |
| `Textarea` | `<textarea>`. `rows`, `value`, `width`, `extra`. |
| `FormLabel` | `<label for="...">`. `inline=False` (default) puts the label above the input (`block mb-1.5`); `inline=True` is for labels beside a control in a flex row. Prop is `target=` not `for=` because `for` is a Python keyword (JinjaX parses `{#def #}` as a Python signature). |
| `ColorSwatch` | Circular `role="radio"` button for the workspace color pickers (settings + create form). `hex` / `name` (aria-label) / `selected` (aria-checked) / `size` (`"md"` 34px settings, `"sm"` 24px create) / `disabled`. Owns the markup contract the picker JS selects on (`.color-swatch`, `aria-checked`, `data-color`); the rim + selection-ring styles live in `tokens.css`. |

### Feedback

| Component | Role |
|---|---|
| `Notice` | Info / warn / success / error banner. Use HTML attribute passthrough (`id=`, `class="hidden"`) for JS-toggled messages. |
| `StatusBadge` | Compact pill. `variant="neutral"` / `success` / `error` / `warn` / `info`. |
| `Spinner` | CSS-only animated circle. `size="sm"` / `"md"` / `"lg"` ; `tone="default"` / `"accent"` (blue, for primary-action spinners). |

### Icons

| Component | Role |
|---|---|
| `Icon24` | 24x24 lucide-style stroke icon. `name=` picks from `ICONS_24` dict in `templates.py`. Sizes `sm` / `md` / `lg`. Inherits color via `currentColor`. |
| `Icon12` | 12x12 title-bar chrome glyph (minimize / maximize / close). Single canonical `w-3 h-3` size; used only inside TitlebarButton `variant="control"`. |
| `auth.OauthIcon` | Brand glyph (Google / GitHub). Stays separate from `Icon24` -- multi-color brand fills, no stroke shell. |

### CSS classes for JS-rendered surfaces

JavaScript can't call JinjaX components. When you build HTML in JS (e.g.
`Landing.jinja`'s providers panel, `sharing.js`'s ACL rows), reference
these CSS-only tokens defined in `static/tokens.css` so both sides stay
in sync:

| Class | Role |
|---|---|
| `.minds-card` | Card surface (bg-white, border-zinc-200, rounded-xl). Match `Card.jinja`. |
| `.spinner` / `.spinner-accent` | Animated circular spinner. Match `Spinner.jinja`. |
| `.code-pill` | Inline `<code>` pill (zinc-100 bg, rounded-md, monospace, 0.95em). Match `Sharing.jinja`'s service-name pills. |
| `.accent-spine` | Vertical workspace-accent stripe on the left edge. Used by Landing project rows + Destroying. |
| `.sidebar-item` | Sidebar workspace row styling. |
| `.titlebar-title` / `.titlebar-btn` / `.titlebar-btn-danger` / `.titlebar-account` | Accent-aware titlebar foreground utilities. Read `--titlebar-fg` with varying alpha for the title / nav-icon / hover-tint hierarchy; `-danger` keeps the destructive red hover regardless of accent. |

## Where the shared tokens live

| Source | Contents |
|---|---|
| `templates.py` | `BTN_BASE` / `BTN_SIZES` / `BTN_VARIANTS` (button shell), `INPUT_BASE` (form-control shell), `ICONS_24` / `ICONS_12` (SVG path data). Exposed as JinjaX Catalog globals. |
| `static/tokens.css` | `.minds-card`, `.spinner` + `.spinner-accent`, `.code-pill`, `.accent-spine`, `.sidebar-item`, `.accent-swatch`, `.color-swatch` / `.color-hex-pill` (workspace color picker rim + selection-ring / hex-input pill), `.titlebar-title` / `.titlebar-btn` / `.titlebar-btn-danger` / `.titlebar-account`, `--shadow-seam`, `--workspace-accent` / `--titlebar-bg` / `--titlebar-fg` (set via inline style on the document root by chrome.js). |
| `templates/pages/DevStyleguide.jinja` | The live visual catalog. Mount at `/_dev/styleguide` in a running app. Tells you what exists and what each variant looks like. |

The type ramp (h1/h2/body/caption sizes), the text-color ramp (the 5
zinc shades and their roles), the corner-radius ramp, and the type
weights are all documented in the styleguide. **Don't** introduce new
zinc shades, radii, or font-weights without a deliberate reason --
existing patterns cover almost every case.

## Visual verification

For changes that touch templates, run the visual-diff harness before
finishing. It captures every rendered scenario via Playwright on two
branches and produces a side-by-side report with a click-through
lightbox:

```bash
# On main:
git checkout main
uv run apps/minds/scripts/visual_diff.py capture --label main

# On your branch:
git checkout your-branch
uv run apps/minds/scripts/visual_diff.py capture --label your-branch

# Compare and open:
uv run apps/minds/scripts/visual_diff.py compare main your-branch
open apps/minds/.visual-diff/report-main-vs-your-branch.html
```

In the report's lightbox: click a thumbnail to open, click the image
to swap A&harr;B, &larr; / &rarr; step between differing scenarios,
Esc closes.

## JinjaX gotchas (these will bite if you don't know them)

### Prop names can't be Python keywords

`{#def #}` is ast-parsed as a Python function signature. `for`,
`class`, `if`, etc. can't be prop names. Workaround: use a synonym
(`target` for the HTML `for` attribute is the established convention
in this codebase).

### No nested `{# #}` comments

Jinja closes the outer comment at the first `#}` it encounters. A
comment containing a literal `{#def #}` token, or a `{# nested #}`
inside another `{# … #}`, will silently leak everything after the
inner `#}` as visible page content. The first symptom is usually that
your component's docstring renders as plain text on every page that
uses it. (See `git log -- Spinner.jinja` for the bug we shipped and
fixed.)

### Literal `<Component>` tags inside `{# #}` docstrings confuse the parser

JinjaX's tag matcher scans component bodies including comment blocks
and treats any `<Component>` it sees as an open tag, looking for the
matching close. If the docstring shows usage like `<Link href="/x">`,
JinjaX may report "Unclosed component Link" or recurse infinitely
when rendering the page that uses your component. Workaround: use
prose ("the Link component") instead of literal angle-bracket
references in docstrings.

### Component attributes are literal strings by default

`<Card href="{{ url }}">` passes the literal string `"{{ url }}"` --
Jinja interpolation does not run inside component-tag attributes.
Prefix with `:` for Python expressions: `<Card :href="url">`. For
multi-piece string composition, precompute with `{% set %}` and pass
the result via `:attr="var"`.

### `attrs.render()` passthrough

Most primitives use `{{ attrs.render(classes=_cls, ...) }}` so callers
can pass arbitrary HTML attributes (`id=`, `data-*`, `title=`,
`onclick=`, etc.) without each one being a declared prop. Classes
passed via `class="..."` on the call site get merged with the
component's own class output.

### `!important` on the link-style ghost-Button recipe

The "ghost Button that looks like a text link" recipe in the styleguide
needs every override prefixed with `!`:

```
extra="!p-0 !bg-transparent !text-xs !font-normal !text-blue-600
       hover:!bg-transparent hover:underline hover:!text-blue-700"
```

The `!` is load-bearing -- the Button base's `font-medium text-sm` and
the ghost variant's `text-zinc-700` have the same Tailwind specificity
as the extras and land earlier in the generated stylesheet, so they
win without `!`. Without it, the "link" reads as a heavy button.

## Where to put new components

- A general-purpose primitive (used by 2+ pages, ideally in different
  flows): root of `templates/`.
- Auth-flow-specific component: `templates/auth/`.
- One-off page that doesn't fit a primitive: `templates/pages/<Name>.jinja`,
  inline its markup, and add a brief docstring explaining what's
  unique about it.
- Live demo for the catalog: add a section to `templates/pages/DevStyleguide.jinja`.
