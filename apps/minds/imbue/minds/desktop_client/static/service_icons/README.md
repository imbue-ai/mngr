# Service icons

Brand marks for the services latchkey can connect to, keyed by latchkey
canonical service name (`<service_id>.svg`). Used by the Permissions tab's
service marks.

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
document with the icon's own `viewBox`; no path data is authored here. Six
marks (`ngrok`, `ramp`, `vercel-mcp` and their `-on-dark` variants) instead
come straight from the vendor's own published brand assets, because no vetted
set carries them (or, for Vercel, carries no white variant) -- see the
brand-kit section of `ATTRIBUTION.md`.

| Source | Package | License |
|---|---|---|
| [SVG Logos](https://github.com/gilbarbara/logos) by Gil Barbara | `@iconify-json/logos` v1.2.12 (v1.2.18 for the two marks that say so below) | CC0-1.0 |
| [selfh.st/icons](https://github.com/selfhst/icons) | `@iconify-json/selfhst` v1.2.13 | CC BY 4.0 |
| [ngrok brand kit](https://ngrok.com/brand) | `ngrok-logos.zip` (official site) | vendor brand assets |
| [Ramp press kit](https://ramp.com/press) | `ramp_june_26_press_kit.zip` (official site) | vendor brand assets |
| [Vercel brand assets](https://vercel.com/geist/brands) | `vercel-triangle-{black,white}.svg` (official asset host) | vendor brand assets |

Two marks are taken from `@iconify-json/logos` v1.2.18 rather than v1.2.12
because v1.2.12 does not carry them. The rest stay pinned to v1.2.12: three
of the Google marks changed between the two releases, and the files here are
the v1.2.12 artwork.

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
| `airtable-mcp.svg` | `logos:airtable` |
| `atlassian-mcp.svg` | `logos:atlassian` |
| `buildkite-mcp.svg` | `logos:buildkite-icon` |
| `cal-com-mcp.svg` | `selfhst:cal-com` |
| `cal-com-mcp-on-dark.svg` | `selfhst:cal-com-light` |
| `fastmail.svg` | `selfhst:fastmail` |
| `huggingface.svg` | `logos:hugging-face-icon` |
| `linktree-mcp.svg` | simple-icons v13.21.0 (CC0-1.0) |
| `neon-mcp.svg` | `logos:neon-icon` |
| `netlify-mcp.svg` | `logos:netlify-icon` |
| `openrouter.svg` | `logos:openrouter-icon` (v1.2.18) |
| `paypal-mcp.svg` | `logos:paypal` |
| `posthog-mcp.svg` | `logos:posthog-icon` |
| `supabase-mcp.svg` | `logos:supabase-icon` |
| `tailscale.svg` | `logos:tailscale` (v1.2.18) |
| `tailscale-on-dark.svg` | `selfhst:tailscale-light` |
| `ticktick-mcp.svg` | `selfhst:ticktick` |
| `trello-mcp.svg` | `selfhst:trello` |
| `vercel-mcp.svg` | Vercel brand assets `vercel-triangle-black.svg` |
| `vercel-mcp-on-dark.svg` | Vercel brand assets `vercel-triangle-white.svg` |
| `webflow-mcp.svg` | simple-icons v13.21.0 (CC0-1.0) |
| `wix-mcp.svg` | simple-icons v13.21.0 (CC0-1.0) |

The brand-kit files carry the vendor's artwork unaltered, with two mechanical
normalizations so they behave like the rest of the set: fixed root
`width`/`height` attributes become the equivalent `viewBox` (ngrok, Vercel),
and a CSS `class` + `<style>` fill becomes the same fill written directly on
the path (Ramp). Path data and colors are byte-identical to the vendor's
files. Vercel's black triangle names no fill at all, exactly as the vendor
publishes it; like Notion's unfilled path it paints the SVG default, black,
which is the brand's color.

Calendly, Umami, Linktree, Webflow and Wix have no color symbol in either
Iconify set — SVG Logos carries Webflow and Wix only as wordmarks, and
the others not at all — so they keep the flat simple-icons silhouette the
whole set used to be. Their fill is written into the file (Calendly `#006BFF`,
Umami `#000000`, Linktree `#43E55E`, Webflow `#146EF5`, Wix `#0C6EFC`, each
brand's own color as simple-icons records it) because an `<img>`-loaded SVG
inherits no color from the page: left implicit, all would paint black.

Several of the vendor marks are single-color because that is how the brand
draws them — GitHub, Linear, Sentry, Dropbox, Discord and Yelp in their brand
ink, Claude in its coral, Coolify in its purple, OpenRouter in its violet,
ngrok, Ramp, Tailscale and Vercel in black or near-black. That is the brand's
choice, not a monochrome treatment applied here.

## Adding a mark

Drop `<service_id>.svg` in, using the vendor's published artwork. Add
`<service_id>-on-dark.svg` only if the mark is illegible on black *and* the
vendor publishes a white variant — do not make one by recoloring. A service
with no file falls back to a plain glyph in the Permissions tab. Eleven catalog
services currently do: Any.do, Attio, beehiiv, Fathom, Gamma, Granola, invideo,
Otter.ai and Readwise appear in no vetted icon set; Peloton only as a
near-black silhouette with no published white variant, so it would vanish on
the dark theme; and Canva only in simple-icons releases before v16, which
removed it because Canva's brand guidelines restrict the logo to authorised
parties and the set never obtained permission, so the mark is not taken from
an older release either. `service_icons_test.py` pins which marks are
single-color
and which ship a dark-surface variant, so a file swapped for a fill-less
silhouette fails there rather than shipping invisible.
