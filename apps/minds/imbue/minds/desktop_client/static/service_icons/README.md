# Service icons

Brand marks for the services latchkey can connect to, keyed by latchkey
canonical service name (`<service_id>.svg`). Used by the Permissions tab's
service marks and by the creating-page onboarding carousel.

These are each vendor's own artwork, in the vendor's own colors. Nothing here
is recolored, and nothing may be: several of these brands publish usage rules
that forbid altering their logo's colors. A mark that cannot be seen on the
dark theme's black surface gets a *second file* instead, `<service>-on-dark.svg`,
which is a separate vendor-published white variant of the same logo — never a
tinted copy of the first.

Because the artwork carries its own color, it must be drawn as an `<img>`
(`ServiceMark.ts`, `.service-mark-img` in `frontend/src/style.css`) and never
inlined into the page. A CSS mask keeps only the alpha channel and would throw
the color away, and inlining `notion-mcp.svg` under a `fill: currentColor` ancestor
would repaint it — it pairs a white path with a deliberately unfilled one.

## Provenance

Most marks come from two Iconify icon sets, copied verbatim from their npm
packages. Each file is an icon body from the set, wrapped as a standalone SVG
document with the icon's own `viewBox`; no path data is authored here. Four
marks (`ngrok`, `ramp` and their `-on-dark` variants) instead come straight
from the vendor's own published brand kit, because no vetted set carries them
-- see the press-kit section of `ATTRIBUTION.md`.

| Source | Package | License |
|---|---|---|
| [SVG Logos](https://github.com/gilbarbara/logos) by Gil Barbara | `@iconify-json/logos` v1.2.12 | CC0-1.0 |
| [selfh.st/icons](https://github.com/selfhst/icons) | `@iconify-json/selfhst` v1.2.13 | CC BY 4.0 |
| [ngrok brand kit](https://ngrok.com/brand) | `ngrok-logos.zip` (official site) | vendor brand assets |
| [Ramp press kit](https://ramp.com/press) | `ramp_june_26_press_kit.zip` (official site) | vendor brand assets |

The CC BY 4.0 set requires attribution wherever the app ships. That lives in
`ATTRIBUTION.md`, beside this file so it travels in the wheel with the artwork.

### Per-file source

| File | Source icon |
|---|---|
| `aws.svg` | `logos:aws` |
| `discord.svg` | `logos:discord-icon` |
| `dropbox.svg` | `logos:dropbox` |
| `figma.svg` | `logos:figma` |
| `github.svg` | `logos:github-icon` |
| `gitlab.svg` | `logos:gitlab-icon` |
| `google-analytics.svg` | `logos:google-analytics` |
| `google-calendar.svg` | `logos:google-calendar` |
| `google-directions.svg` | `logos:google-maps` |
| `google-drive.svg` | `logos:google-drive` |
| `google-gmail.svg` | `logos:google-gmail` |
| `linear.svg` | `logos:linear-icon` |
| `mailchimp.svg` | `logos:mailchimp-freddie` |
| `notion-mcp.svg` | `logos:notion-icon` |
| `sentry.svg` | `logos:sentry-icon` |
| `slack.svg` | `logos:slack-icon` |
| `stripe.svg` | `selfhst:stripe` |
| `telegram.svg` | `logos:telegram` |
| `todoist.svg` | `logos:todoist-icon` |
| `zoom.svg` | `logos:zoom-icon` |
| `google-docs.svg` | `selfhst:google-docs` |
| `google-people.svg` | `selfhst:google-contacts` |
| `google-sheets.svg` | `selfhst:google-sheets` |
| `google-slides.svg` | `selfhst:google-slides` |
| `yelp.svg` | `selfhst:yelp` |
| `aws-on-dark.svg` | `selfhst:amazon-web-services-light` |
| `github-on-dark.svg` | `selfhst:github-light` |
| `linear-on-dark.svg` | `selfhst:linear-light` |
| `sentry-on-dark.svg` | `selfhst:sentry-light` |
| `umami-on-dark.svg` | `selfhst:umami-light` |
| `claude-ai.svg` | `logos:claude-icon` |
| `coolify.svg` | `selfhst:coolify` |
| `ngrok.svg` | ngrok brand kit `ngrok-n-dark.svg` |
| `ngrok-on-dark.svg` | ngrok brand kit `ngrok-n-white.svg` |
| `ramp.svg` | Ramp press kit `Ramp-Symbol-RGB-Slate-0125.svg` |
| `ramp-on-dark.svg` | Ramp press kit `Ramp-Symbol-RGB-White.svg` |
| `calendly.svg` | simple-icons v13.21.0 (CC0-1.0) |
| `umami.svg` | simple-icons v13.21.0 (CC0-1.0) |

The press-kit files carry the vendor's artwork unaltered, with two mechanical
normalizations so they behave like the rest of the set: fixed root
`width`/`height` attributes become the equivalent `viewBox` (ngrok), and a CSS
`class` + `<style>` fill becomes the same fill written directly on the path
(Ramp). Path data and colors are byte-identical to the vendor's files.

Calendly and Umami have no color artwork in either Iconify set — both are
`palette: false` everywhere they appear — so they keep the flat simple-icons
silhouette the whole set used to be. Their fill is written into the file
(Calendly `#006BFF`, Umami `#000000`, each brand's own color as simple-icons
records it) because an `<img>`-loaded SVG inherits no color from the page: left
implicit, both would paint black.

Several of the vendor marks are single-color because that is how the brand
draws them — GitHub, Linear, Sentry, Dropbox, Discord and Yelp in their brand
ink, Claude in its coral, Coolify in its purple, ngrok and Ramp in near-black.
That is the brand's choice, not a monochrome treatment applied here.

## Adding a mark

Drop `<service_id>.svg` in, using the vendor's published artwork. Add
`<service_id>-on-dark.svg` only if the mark is illegible on black *and* the
vendor publishes a white variant — do not make one by recoloring. A service
with no file falls back to a plain glyph in the Permissions tab and a monogram
in the carousel (every current catalog service has one). `service_icons_test.py` pins
which marks are single-color and which ship a dark-surface variant, so a file
swapped for a fill-less silhouette fails there rather than shipping invisible.
