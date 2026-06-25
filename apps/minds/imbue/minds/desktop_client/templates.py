"""HTML rendering for the desktop client.

Each ``render_*`` function is a thin wrapper around a JinjaX component
under ``templates/`` in this directory, rendered through the shared
``CATALOG``. Primitive components (Button, Card, Notice, Spinner,
TextInput, Opt, ...) and the page layout (``Base``) sit at the top of
``templates/``; full pages live under ``templates/pages/`` as PascalCase
``.jinja`` files; auth pages and the OAuth icon component live under
``templates/auth/``. Tests call these functions directly; the FastAPI
route handlers call them the same way. The public signatures are stable
so neither callers nor tests have to know the templates moved from raw
Jinja2 macros + ``{% extends %}`` to JinjaX components.
"""

import html
import os
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from jinja2 import Environment
from jinja2 import select_autoescape
from jinjax import Catalog

from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.agent_creator import AgentCreationInfo
from imbue.minds.desktop_client.onboarding import expected_creation_duration_seconds
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.desktop_client.workspace_color import WORKSPACE_PALETTE
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import BackupEncryptionMethod
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.minds.utils.sentry.frontend import frontend_sentry_browser_payload
from imbue.mngr.primitives import AgentId
from imbue.mngr_forward.loading_page import render_loading_page

TEMPLATE_DIR: Final[Path] = Path(__file__).resolve().parent / "templates"

# Shared Tailwind class strings for the three button components
# (Button.jinja, ButtonLink.jinja, ButtonSubmit.jinja). Exposed as JinjaX
# Catalog globals so a single edit here updates every button variant; the
# alternative -- inlining the same class string in three sibling templates
# -- drifted across files trivially. Surface as uppercase to match the
# `CATALOG` constant convention and to mark them as Jinja globals (not
# per-render context).
#
# Size axis is independent of variant -- size dictates geometry (padding,
# radius, font weight, text size), variant dictates color. ``md`` is the
# default in-flow button; ``lg`` is the prominent block CTA used on the
# auth flow; ``icon`` is a square padding for icon-only buttons (e.g. the
# restart / settings icons in the Landing project row).
# The focus ring is an outline OUTSIDE the button (outline-offset) so it never
# overwrites the variant border; the offset gap is transparent (shows the
# background) in every mode. focus-visible keeps it to keyboard focus. Pressing
# nudges the whole button to 98% scale -- animated over 100ms on the standard
# ease-in-out curve (``cubic-bezier(0.4, 0, 0.2, 1)``) -- for a tactile click
# across every variant. The animation is scoped to ``transition-transform`` so
# only the press scale eases; hover/press color + opacity changes flip instantly.
_BTN_BASE: Final[str] = (
    "inline-flex items-center justify-center gap-1.5 leading-tight "
    "transition-transform duration-100 ease-in-out disabled:opacity-40 disabled:cursor-not-allowed "
    "cursor-pointer no-underline whitespace-nowrap active:scale-[0.98] "
    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
)
# All sizes share the md control radius (rounded-md = 6px); they differ only in
# padding and (for icon) shape.
_BTN_SIZES: Final[Mapping[str, str]] = {
    "md": "px-4 py-2 rounded-md type-label",
    "lg": "px-4 py-3 rounded-md type-label",
    "icon": "p-1.5 rounded-md type-label",
}
# Variant recipes (Figma "Button" component, node 342-4059). Every variant
# carries a 1px border -- visible on secondary, transparent elsewhere -- so all
# variants share the exact same box height (border-box) regardless of border.
# Solid variants (primary / danger / success) dim via opacity on hover; the
# no-fill variants (secondary / ghost) tint with the fill tokens on hover. The
# press (active) state carries no color change -- only the shared scale-down.
_BTN_VARIANTS: Final[Mapping[str, str]] = {
    "primary": "bg-surface-inverse text-inverse-primary border border-transparent hover:opacity-80",
    "secondary": "bg-transparent text-primary border border-default hover:bg-fill-hover",
    "danger": "bg-important text-white border border-transparent hover:opacity-90",
    "success": "bg-success text-white border border-transparent hover:opacity-90",
    "ghost": "bg-transparent text-primary border border-transparent hover:bg-fill-hover",
}

# Shared Tailwind class string for the three form-control components
# (TextInput.jinja, Select.jinja, Textarea.jinja). Exposed as a Catalog
# global so the focus ring, border, padding and text size live in exactly one
# place. Width, border-radius and line-height vary per-component so they are NOT
# included here -- each sets its own (the single-line TextInput / Select add
# ``leading-tight``; Textarea keeps ``type-body``'s roomier 1.5 leading so its
# wrapped lines stay legible). Matches Figma's text field (node 345-4059): 8px
# padding, a tertiary placeholder, and a border-strong edge that darkens to
# border-stronger on hover (a quieter cue than a fill tint), with a focus ring
# drawn OUTSIDE the field (outline-offset) so it keeps the border rather than
# recoloring it.
_INPUT_BASE: Final[str] = (
    "p-2 type-body border border-strong bg-surface-primary text-primary "
    "placeholder:text-tertiary hover:border-stronger "
    "focus:outline-2 focus:outline-offset-2 focus:outline-accent"
)

# Inner SVG markup for the 16x16 icon set (Figma "Icon" frame, node
# 857-5091). Each glyph is rendered by Icon16.jinja, which wraps this in a
# 16x16-viewBox <svg> defaulting to fill="currentColor" -- so every icon
# inherits the parent's text color instead of Figma's hardcoded black. Most
# glyphs are filled outlines (Figma "Vector (Stroke)" flattened to a single
# fill path); ``play`` is the lone stroked glyph and carries its own
# fill=none stroke=currentColor to match the set's line weight. ``settings``
# (drawn on a 15-unit grid) and ``chevron-down-small`` (a small centered
# glyph) are nudged into the 16-unit frame with a <g transform>. The dict is
# the single source of truth -- to add or swap an icon, edit one entry here.
_ICONS_16: Final[Mapping[str, str]] = {
    "menu": '<path d="M13.3337 11.4004C13.6649 11.4006 13.9333 11.6687 13.9333 12C13.9333 12.3313 13.6649 12.5994 13.3337 12.5996H2.66667C2.3353 12.5996 2.06706 12.3314 2.06706 12C2.06706 11.6686 2.3353 11.4004 2.66667 11.4004H13.3337ZM13.3337 7.40039C13.6649 7.40057 13.9333 7.66874 13.9333 8C13.9333 8.33126 13.6649 8.59943 13.3337 8.59961H2.66667C2.3353 8.59961 2.06706 8.33137 2.06706 8C2.06706 7.66863 2.3353 7.40039 2.66667 7.40039H13.3337ZM13.3337 3.40039C13.6649 3.40057 13.9333 3.66874 13.9333 4C13.9333 4.33126 13.6649 4.59943 13.3337 4.59961H2.66667C2.3353 4.59961 2.06706 4.33137 2.06706 4C2.06706 3.66863 2.3353 3.40039 2.66667 3.40039H13.3337Z"/>',
    "home": '<path d="M9.40039 9.01301C9.40039 8.99548 9.39316 8.9786 9.38086 8.96613C9.36836 8.95363 9.35069 8.9466 9.33301 8.9466H6.66699C6.64931 8.9466 6.63164 8.95363 6.61914 8.96613C6.60684 8.9786 6.59961 8.99548 6.59961 9.01301V13.7464H9.40039V9.01301ZM10.5996 13.7464H12.667C12.8614 13.7463 13.0481 13.669 13.1855 13.5316C13.323 13.3941 13.4004 13.2074 13.4004 13.013V7.01301C13.4004 6.90648 13.3768 6.80107 13.332 6.70441C13.2871 6.60765 13.2211 6.52132 13.1396 6.45246L13.1367 6.44953L8.47363 2.45246V2.45344C8.34127 2.34157 8.1733 2.27961 8 2.27961C7.8267 2.27961 7.65873 2.34157 7.52637 2.45344L7.52539 2.45246L2.86328 6.44953L2.86035 6.45246C2.77888 6.52132 2.71287 6.60765 2.66797 6.70441C2.62319 6.80107 2.59958 6.90648 2.59961 7.01301V13.013C2.59962 13.2074 2.67703 13.3941 2.81445 13.5316C2.9519 13.669 3.13863 13.7463 3.33301 13.7464H5.40039V9.01301C5.40039 8.67707 5.53394 8.35505 5.77148 8.1175C6.00901 7.88006 6.33113 7.74641 6.66699 7.74641H9.33301C9.66887 7.74641 9.99099 7.88006 10.2285 8.1175C10.4661 8.35505 10.5996 8.67707 10.5996 9.01301V13.7464ZM14.5996 13.013C14.5996 13.5257 14.3966 14.0176 14.0342 14.3802C13.6717 14.7427 13.1796 14.9465 12.667 14.9466H3.33301C2.82037 14.9465 2.32831 14.7427 1.96582 14.3802C1.60335 14.0176 1.4004 13.5257 1.40039 13.013V7.01301C1.40034 6.73181 1.46172 6.45363 1.58008 6.19855C1.69787 5.94487 1.86881 5.71937 2.08203 5.5384L6.74902 1.53937L6.75195 1.53645C7.10089 1.24157 7.54315 1.08039 8 1.08039C8.39979 1.08039 8.78863 1.20339 9.11328 1.43195L9.24805 1.53645L9.25098 1.53937L13.918 5.5384H13.917C14.1305 5.71946 14.302 5.94465 14.4199 6.19855C14.5383 6.45363 14.5997 6.73182 14.5996 7.01301V13.013Z"/>',
    "user": '<path d="M10.0667 6.66634C10.0666 5.52521 9.14146 4.60011 8.00033 4.59993C6.85905 4.59993 5.93312 5.5251 5.93294 6.66634C5.93294 7.73631 6.7464 8.61703 7.78841 8.72298L8.00033 8.73372L8.21126 8.72298C9.25343 8.61718 10.0667 7.73642 10.0667 6.66634ZM8.00033 9.93294C7.09867 9.93294 6.23364 10.2915 5.59603 10.929C5.02946 11.4956 4.68368 12.2417 4.61361 13.0335C5.58084 13.6855 6.74612 14.0667 8.00033 14.0667C9.25402 14.0667 10.4181 13.6851 11.3851 13.0335C11.315 12.2418 10.9711 11.4956 10.4046 10.929C9.76708 10.2915 8.90194 9.93303 8.00033 9.93294ZM15.2669 8.00033C15.2668 12.0133 12.0133 15.2668 8.00033 15.2669C3.98717 15.2669 0.7339 12.0134 0.733724 8.00033C0.733724 3.98706 3.98706 0.733724 8.00033 0.733724C12.0134 0.7339 15.2669 3.98717 15.2669 8.00033ZM11.2669 6.66634C11.2669 7.69667 10.7885 8.6145 10.0433 9.21322C10.4865 9.43301 10.8958 9.72402 11.2523 10.0804C11.8255 10.6536 12.2307 11.3631 12.4388 12.1322C13.4477 11.0489 14.0666 9.59743 14.0667 8.00033C14.0667 4.64991 11.3507 1.93312 8.00033 1.93294C4.6498 1.93294 1.93294 4.6498 1.93294 8.00033C1.93303 9.59711 2.55134 11.0489 3.5599 12.1322C3.76801 11.363 4.17413 10.6537 4.7474 10.0804C5.10361 9.72422 5.51251 9.43299 5.9554 9.21322C5.21063 8.61448 4.73372 7.69631 4.73372 6.66634C4.7339 4.86236 6.1963 3.39974 8.00033 3.39974C9.8042 3.39992 11.2668 4.86247 11.2669 6.66634Z"/>',
    "inbox": '<path d="M14.0667 8.60026H10.9876L9.83236 10.3327C9.72108 10.4996 9.53394 10.6003 9.33333 10.6003H6.66634C6.46585 10.6002 6.27854 10.4995 6.16732 10.3327L5.01302 8.60026H1.93294V11.9997C1.93294 12.194 2.01046 12.3807 2.14779 12.5182C2.28524 12.6557 2.47197 12.733 2.66634 12.7331H13.3333C13.5278 12.7331 13.7144 12.6558 13.8519 12.5182C13.9893 12.3807 14.0667 12.1941 14.0667 11.9997V8.60026ZM4.8265 3.26628C4.69024 3.26644 4.55656 3.30481 4.44076 3.37663C4.32483 3.44853 4.23095 3.55134 4.17025 3.6735V3.67546L2.30404 7.40007H5.33333L5.40755 7.40495C5.57941 7.42637 5.73495 7.52153 5.83236 7.66764L6.98763 9.40007H9.01302L10.1673 7.66764L10.2122 7.60807C10.3253 7.47693 10.4908 7.40016 10.6663 7.40007H13.6956L11.8294 3.6735C11.7687 3.55137 11.6748 3.44851 11.5589 3.37663C11.4721 3.3228 11.3752 3.28822 11.2747 3.27409L11.1732 3.26628H4.8265ZM15.2669 11.9997C15.2669 12.5123 15.063 13.0043 14.7005 13.3669C14.338 13.7294 13.8461 13.9333 13.3333 13.9333H2.66634C2.15371 13.9332 1.66165 13.7294 1.29915 13.3669C0.936787 13.0043 0.733724 12.5123 0.733724 11.9997V7.99967C0.733775 7.90652 0.755492 7.81442 0.797201 7.73112L3.09701 3.13835C3.25708 2.81688 3.5037 2.54637 3.80892 2.3571C4.11454 2.16764 4.46691 2.06725 4.8265 2.06706H11.1732C11.5329 2.06725 11.8861 2.16754 12.1917 2.3571C12.4968 2.54633 12.7426 2.81702 12.9027 3.13835L15.2035 7.73112C15.2452 7.81442 15.2669 7.90652 15.2669 7.99967V11.9997Z"/>',
    "settings": '<g transform="translate(0.5 0.5)"><path d="M8.33765 2.50001C8.33765 2.31436 8.26384 2.13617 8.13257 2.00489C8.0013 1.87363 7.82309 1.79981 7.63745 1.79981H7.36206C7.17669 1.79993 6.99907 1.87389 6.86792 2.00489C6.73664 2.13617 6.66284 2.31436 6.66284 2.50001V2.61329C6.66248 2.92882 6.57854 3.23854 6.42065 3.51173C6.263 3.78448 6.03608 4.01019 5.76343 4.16798L5.7644 4.16895L5.49487 4.3252L5.4939 4.32618C5.22026 4.48416 4.90947 4.56739 4.59351 4.56739C4.28352 4.56735 3.97951 4.48625 3.70972 4.33399V4.33497L3.61597 4.28517C3.61069 4.28235 3.60552 4.27936 3.60034 4.27638C3.43989 4.18382 3.24904 4.15837 3.07007 4.20606C2.89093 4.25397 2.73723 4.37177 2.64429 4.53224L2.50757 4.76954C2.41531 4.92995 2.39048 5.12004 2.43823 5.29884C2.47414 5.43311 2.54837 5.553 2.65112 5.64356L2.76343 5.72364L2.79272 5.7422L2.88647 5.8047H2.8855C3.14427 5.9608 3.36037 6.17885 3.51245 6.44044C3.67027 6.71193 3.75466 7.01998 3.75659 7.33399V7.65431L3.75269 7.77247C3.73559 8.04849 3.65566 8.31791 3.51733 8.5586C3.3607 8.83108 3.13439 9.05648 2.86304 9.21485L2.86401 9.21583L2.77026 9.27149L2.76343 9.27638C2.60297 9.36932 2.48613 9.52204 2.43823 9.70118C2.39048 9.87998 2.41531 10.0701 2.50757 10.2305L2.64429 10.4678C2.73723 10.6282 2.89093 10.7461 3.07007 10.794C3.24904 10.8417 3.43989 10.8162 3.60034 10.7236L3.61597 10.7149L3.70972 10.665C3.97941 10.5129 4.28368 10.4327 4.59351 10.4326C4.86998 10.4326 5.14235 10.4964 5.3894 10.6182L5.4939 10.6738L5.49487 10.6748L5.76245 10.8301L5.86304 10.8926C6.09166 11.0455 6.28249 11.2493 6.42065 11.4883C6.57854 11.7615 6.66248 12.0712 6.66284 12.3867V12.5C6.66284 12.6857 6.73664 12.8639 6.86792 12.9951C6.99907 13.1261 7.17669 13.2001 7.36206 13.2002H7.63745C7.82309 13.2002 8.0013 13.1264 8.13257 12.9951C8.26384 12.8639 8.33765 12.6857 8.33765 12.5V12.3867C8.33801 12.0713 8.42105 11.7614 8.57886 11.4883C8.71709 11.2492 8.90868 11.0455 9.13745 10.8926L9.23706 10.8301L9.50464 10.6748L9.50659 10.6738C9.78011 10.516 10.0902 10.4327 10.406 10.4326C10.7158 10.4326 11.0201 10.513 11.2898 10.665L11.3835 10.7149L11.4001 10.7236C11.5607 10.8162 11.7514 10.8418 11.9304 10.794C12.1096 10.7461 12.2623 10.6282 12.3552 10.4678L12.49 10.2295L12.4919 10.2256C12.5846 10.065 12.6102 9.87349 12.5623 9.69435C12.5148 9.51717 12.3998 9.36564 12.2419 9.27247L12.1599 9.2295C12.1545 9.2266 12.1487 9.22282 12.1433 9.21974C11.869 9.06123 11.6411 8.83328 11.4832 8.5586C11.3251 8.28367 11.2427 7.9714 11.2439 7.65431V7.34376C11.243 7.02734 11.3255 6.71579 11.4832 6.44142C11.6397 6.16926 11.8645 5.9425 12.1355 5.78419L12.2292 5.72853L12.2371 5.72364C12.3973 5.63068 12.5144 5.47785 12.5623 5.29884C12.61 5.11979 12.5845 4.92909 12.4919 4.76856V4.76759L12.3552 4.53224C12.2623 4.37177 12.1096 4.25397 11.9304 4.20606C11.7514 4.15823 11.5607 4.18386 11.4001 4.27638C11.3949 4.27942 11.3889 4.2823 11.3835 4.28517L11.3054 4.3252L11.3064 4.32618C11.0328 4.48416 10.722 4.56739 10.406 4.56739C10.0902 4.56735 9.78011 4.48405 9.50659 4.32618L9.50464 4.3252L9.23706 4.16895V4.16993C8.96387 4.01211 8.73675 3.78488 8.57886 3.51173C8.42105 3.23859 8.33801 2.92874 8.33765 2.61329V2.50001ZM8.82495 7.50001C8.82495 6.76823 8.23153 6.17481 7.49976 6.17481C6.76809 6.17495 6.17456 6.76831 6.17456 7.50001C6.17456 8.23171 6.76809 8.82507 7.49976 8.8252C8.23153 8.8252 8.82495 8.23179 8.82495 7.50001ZM9.92456 7.50001C9.92456 8.8393 8.83905 9.92481 7.49976 9.92481C6.16058 9.92468 5.07495 8.83922 5.07495 7.50001C5.07495 6.1608 6.16058 5.07534 7.49976 5.0752C8.83905 5.0752 9.92456 6.16072 9.92456 7.50001ZM9.43726 2.61231L9.44312 2.70313C9.45513 2.79393 9.48488 2.88212 9.53101 2.96192C9.57708 3.0416 9.63913 3.11035 9.71167 3.16603L9.78784 3.21778L9.78882 3.21876L10.0554 3.37403H10.0564C10.1627 3.43533 10.2833 3.46774 10.406 3.46778C10.5289 3.46778 10.6502 3.43547 10.7566 3.37403L10.7722 3.36427L10.866 3.31446C11.2757 3.08362 11.7598 3.02198 12.2146 3.14356C12.6753 3.26674 13.0684 3.56786 13.3074 3.98048L13.4451 4.21778V4.21876C13.6833 4.63172 13.7478 5.12244 13.6248 5.58302C13.5023 6.04104 13.2037 6.43153 12.7947 6.67091L12.7019 6.72755L12.6941 6.73243C12.5873 6.79411 12.4987 6.8833 12.4373 6.99024C12.3758 7.09719 12.343 7.21846 12.3435 7.34181V7.65821C12.343 7.78156 12.3758 7.90283 12.4373 8.00978C12.4987 8.11672 12.5873 8.20591 12.6941 8.26759L12.7712 8.3086L12.7878 8.31739C13.2004 8.55634 13.5015 8.94964 13.6248 9.41017C13.7475 9.86904 13.683 10.3575 13.447 10.7695L13.3103 11.0137L13.3074 11.0195C13.0684 11.4322 12.6753 11.7333 12.2146 11.8565C11.7597 11.9781 11.2758 11.9156 10.866 11.6846L10.7722 11.6358C10.7668 11.6329 10.7619 11.629 10.7566 11.626C10.6502 11.5645 10.5289 11.5322 10.406 11.5322C10.2833 11.5323 10.1627 11.5647 10.0564 11.626L10.0554 11.625L9.78882 11.7813L9.78784 11.7822C9.68156 11.8436 9.59244 11.9319 9.53101 12.0381C9.46964 12.1443 9.43745 12.2651 9.43726 12.3877V12.5C9.43726 12.9774 9.24748 13.4349 8.90991 13.7725C8.57235 14.11 8.11483 14.2998 7.63745 14.2998H7.36206C6.88483 14.2997 6.42706 14.1099 6.0896 13.7725C5.75217 13.4349 5.56226 12.9773 5.56226 12.5V12.3877C5.56207 12.2651 5.52988 12.1443 5.46851 12.0381C5.40708 11.932 5.31885 11.8436 5.21265 11.7822L5.21069 11.7813L4.94409 11.626L4.86108 11.5859C4.77655 11.551 4.68557 11.5322 4.59351 11.5322C4.47081 11.5323 4.35018 11.5647 4.2439 11.626C4.23855 11.6291 4.23274 11.6328 4.22729 11.6358L4.13354 11.6856L4.13257 11.6846C3.72316 11.915 3.24026 11.9778 2.78589 11.8565C2.32532 11.7333 1.93212 11.4321 1.69312 11.0195L1.55542 10.7822L1.55444 10.7813C1.31626 10.3683 1.25172 9.87753 1.37476 9.417C1.49724 8.95896 1.79581 8.56751 2.20483 8.32813L2.29858 8.27247L2.3064 8.26759C2.4132 8.20592 2.50178 8.11671 2.56323 8.00978C2.62462 7.90288 2.6565 7.78148 2.65601 7.65821V7.34083L2.65015 7.25001C2.63778 7.15974 2.60737 7.07245 2.56128 6.99317C2.49994 6.88771 2.41203 6.80032 2.3064 6.73927C2.29617 6.73336 2.28595 6.72629 2.27612 6.71974L2.18237 6.65724V6.65626C1.78541 6.4158 1.49488 6.03226 1.37476 5.58302C1.25172 5.12249 1.31626 4.63168 1.55444 4.21876L1.55542 4.21778L1.69312 3.98048C1.93212 3.56797 2.32532 3.26672 2.78589 3.14356C3.2401 3.02225 3.72325 3.08422 4.13257 3.31446H4.13354L4.22729 3.36427C4.23274 3.36717 4.23855 3.37095 4.2439 3.37403C4.35018 3.43533 4.47081 3.46774 4.59351 3.46778C4.71638 3.46778 4.83768 3.43547 4.94409 3.37403L5.21069 3.21876L5.21265 3.21778C5.31885 3.15646 5.40708 3.06806 5.46851 2.96192C5.51463 2.88214 5.54437 2.79391 5.5564 2.70313L5.56226 2.61231V2.50001C5.56226 2.02272 5.75217 1.56509 6.0896 1.22755C6.42706 0.890087 6.88483 0.700322 7.36206 0.700205H7.63745C8.11483 0.700205 8.57235 0.889999 8.90991 1.22755C9.24748 1.56511 9.43726 2.02262 9.43726 2.50001V2.61231Z"/></g>',
    "chevron-right": '<path d="M5.57617 3.57617C5.81049 3.34186 6.18951 3.34186 6.42383 3.57617L10.4238 7.57617C10.6581 7.81049 10.6581 8.18951 10.4238 8.42383L6.42383 12.4238C6.18951 12.6581 5.81049 12.6581 5.57617 12.4238C5.34186 12.1895 5.34186 11.8105 5.57617 11.5762L9.15234 8L5.57617 4.42383C5.34186 4.18951 5.34186 3.81049 5.57617 3.57617Z"/>',
    "chevron-left": '<path d="M9.57617 3.57617C9.81049 3.34186 10.1895 3.34186 10.4238 3.57617C10.6581 3.81049 10.6581 4.18951 10.4238 4.42383L6.84766 8L10.4238 11.5762C10.6581 11.8105 10.6581 12.1895 10.4238 12.4238C10.1895 12.6581 9.81049 12.6581 9.57617 12.4238L5.57617 8.42383C5.34186 8.18951 5.34186 7.81049 5.57617 7.57617L9.57617 3.57617Z"/>',
    "chevron-down": '<path d="M11.5762 5.57617C11.8105 5.34186 12.1895 5.34186 12.4238 5.57617C12.6581 5.81049 12.6581 6.18951 12.4238 6.42383L8.42383 10.4238C8.18951 10.6581 7.81049 10.6581 7.57617 10.4238L3.57617 6.42383C3.34186 6.18951 3.34186 5.81049 3.57617 5.57617C3.81049 5.34186 4.18951 5.34186 4.42383 5.57617L8 9.15234L11.5762 5.57617Z"/>',
    "chevron-up": '<path d="M7.66992 5.49902C7.90282 5.34523 8.21879 5.37114 8.42383 5.57617L12.4238 9.57617C12.6581 9.81049 12.6581 10.1895 12.4238 10.4238C12.1895 10.6581 11.8105 10.6581 11.5762 10.4238L8 6.84766L4.42383 10.4238C4.18951 10.6581 3.81049 10.6581 3.57617 10.4238C3.34186 10.1895 3.34186 9.81049 3.57617 9.57617L7.57617 5.57617L7.66992 5.49902Z"/>',
    "chevron-down-small": '<g transform="translate(4.4004 5.9004)"><path d="M6.17574 0.175736C6.41005 -0.0585787 6.78908 -0.0585787 7.02339 0.175736C7.25771 0.410051 7.25771 0.789078 7.02339 1.02339L4.02339 4.02339C3.78908 4.25771 3.41005 4.25771 3.17574 4.02339L0.175736 1.02339C-0.0585787 0.789078 -0.0585787 0.410051 0.175736 0.175736C0.410051 -0.0585787 0.789078 -0.0585787 1.02339 0.175736L3.59956 2.75191L6.17574 0.175736Z"/></g>',
    "plus": '<path d="M7.39974 12.6663V8.59993H3.33333C3.00207 8.59993 2.7339 8.33155 2.73372 8.00033C2.73372 7.66895 3.00196 7.39974 3.33333 7.39974H7.39974V3.33333C7.39974 3.00196 7.66895 2.73372 8.00033 2.73372C8.33155 2.7339 8.59993 3.00207 8.59993 3.33333V7.39974H12.6663C12.9977 7.39974 13.2669 7.66895 13.2669 8.00033C13.2668 8.33155 12.9976 8.59993 12.6663 8.59993H8.59993V12.6663C8.59993 12.9976 8.33155 13.2668 8.00033 13.2669C7.66895 13.2669 7.39974 12.9977 7.39974 12.6663Z"/>',
    "close": '<path d="M11.5762 3.57617C11.8105 3.34186 12.1895 3.34186 12.4238 3.57617C12.6581 3.81049 12.6581 4.18951 12.4238 4.42383L8.84766 8L12.4238 11.5762C12.6581 11.8105 12.6581 12.1895 12.4238 12.4238C12.1895 12.6581 11.8105 12.6581 11.5762 12.4238L8 8.84766L4.42383 12.4238C4.18951 12.6581 3.81049 12.6581 3.57617 12.4238C3.34186 12.1895 3.34186 11.8105 3.57617 11.5762L7.15234 8L3.57617 4.42383C3.34186 4.18951 3.34186 3.81049 3.57617 3.57617C3.81049 3.34186 4.18951 3.34186 4.42383 3.57617L8 7.15234L11.5762 3.57617Z"/>',
    "restart": '<path d="M8 1.9502C9.58084 1.9502 11.0933 2.53791 12.2695 3.56641L12.5 3.77832L12.5078 3.78516L12.9502 4.22754V2.5C12.9502 2.19625 13.1962 1.9502 13.5 1.9502C13.8038 1.9502 14.0498 2.19625 14.0498 2.5V5.55567C14.0498 5.7015 13.9918 5.84122 13.8887 5.94434C13.7855 6.04745 13.6458 6.10547 13.5 6.10547H10.4443C10.1407 6.10541 9.89459 5.85934 9.89453 5.55567C9.89453 5.25194 10.1406 5.00592 10.4443 5.00586H12.1729L11.7373 4.57031C10.7307 3.60293 9.39357 3.04981 8 3.04981C7.02109 3.04981 6.06397 3.33999 5.25 3.88379C4.43604 4.42766 3.80142 5.20106 3.42676 6.10547C3.05211 7.00996 2.95451 8.00562 3.14551 8.96582C3.33653 9.92595 3.80778 10.8078 4.5 11.5C5.19222 12.1922 6.07405 12.6635 7.03418 12.8545C7.99439 13.0455 8.99004 12.9479 9.89453 12.5732C10.7989 12.1986 11.5723 11.564 12.1162 10.75C12.66 9.93603 12.9502 8.97892 12.9502 8C12.9502 7.69624 13.1962 7.4502 13.5 7.4502C13.8038 7.4502 14.0498 7.69624 14.0498 8C14.0498 9.19658 13.6951 10.3664 13.0303 11.3613C12.3655 12.3562 11.4209 13.132 10.3154 13.5898C9.20994 14.0478 7.99292 14.167 6.81934 13.9336C5.6459 13.7001 4.5677 13.1243 3.72168 12.2783C2.87567 11.4323 2.2999 10.3541 2.06641 9.18067C1.83297 8.00708 1.95225 6.79006 2.41016 5.68457C2.86805 4.57915 3.64384 3.6345 4.63867 2.96973C5.63359 2.30495 6.80342 1.9502 8 1.9502Z"/>',
    "arrow-up-right": '<path d="M12.9331 10.3336C12.9329 10.6648 12.6646 10.9331 12.3335 10.9333C12.0022 10.9333 11.7331 10.6649 11.7329 10.3336V5.1149L4.09033 12.7575C3.85606 12.9916 3.47695 12.9916 3.24268 12.7575C3.00836 12.5232 3.00836 12.1432 3.24268 11.9088L10.8853 4.26627H5.6665C5.33513 4.26627 5.06689 3.99803 5.06689 3.66666C5.06689 3.33529 5.33513 3.06705 5.6665 3.06705H12.3335C12.6647 3.06722 12.9331 3.33539 12.9331 3.66666V10.3336Z"/>',
    "check": '<path d="M12.8737 3.54004C13.1274 3.28647 13.5388 3.28658 13.7926 3.54004C14.0465 3.79388 14.0465 4.20612 13.7926 4.45996L6.45964 11.793C6.20585 12.0468 5.79454 12.0466 5.54069 11.793L2.20671 8.45996C1.95287 8.20612 1.95287 7.79388 2.20671 7.54004C2.46055 7.2862 2.87279 7.2862 3.12663 7.54004L5.99967 10.4141L12.8737 3.54004Z"/>',
    "play": '<path d="M4 2.44155C4 2.24722 4.21199 2.1272 4.37862 2.22717L13.6427 7.78563C13.8045 7.88273 13.8045 8.11727 13.6427 8.21437L4.37862 13.7728C4.21199 13.8728 4 13.7528 4 13.5585V2.44155Z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>',
    "pause": '<path d="M6.06641 3.33366C6.06641 3.29684 6.03682 3.26628 6 3.26628H4.66699C4.63017 3.26628 4.59961 3.29684 4.59961 3.33366V12.6667C4.59961 12.7035 4.63017 12.7331 4.66699 12.7331H6C6.03682 12.7331 6.06641 12.7035 6.06641 12.6667V3.33366ZM11.4004 3.33366C11.4004 3.29684 11.3698 3.26628 11.333 3.26628H10C9.96318 3.26628 9.93359 3.29684 9.93359 3.33366V12.6667C9.93359 12.7035 9.96318 12.7331 10 12.7331H11.333C11.3698 12.7331 11.4004 12.7035 11.4004 12.6667V3.33366ZM7.2666 12.6667C7.2666 13.3662 6.69956 13.9333 6 13.9333H4.66699C3.96743 13.9333 3.40039 13.3662 3.40039 12.6667V3.33366C3.40039 2.6341 3.96743 2.06706 4.66699 2.06706H6C6.69956 2.06706 7.2666 2.6341 7.2666 3.33366V12.6667ZM12.5996 12.6667C12.5996 13.3662 12.0326 13.9333 11.333 13.9333H10C9.30044 13.9333 8.7334 13.3662 8.7334 12.6667V3.33366C8.7334 2.6341 9.30044 2.06706 10 2.06706H11.333C12.0326 2.06706 12.5996 2.6341 12.5996 3.33366V12.6667Z"/>',
}

# 12x12 chrome glyph path data (minimize / maximize / close). Title-bar
# window controls only; rendered through Icon12.jinja, which wraps these in
# its own stroke shell (fill=none, stroke=currentColor) at a 12x12 viewBox.
_ICONS_12: Final[Mapping[str, str]] = {
    "minimize": '<line x1="2" y1="6" x2="10" y2="6"/>',
    "maximize": '<rect x="2" y="2" width="8" height="8" rx="0.5"/>',
    "close": '<line x1="2" y1="2" x2="10" y2="10"/><line x1="10" y1="2" x2="2" y2="10"/>',
}


def _build_catalog() -> Catalog:
    """Build the JinjaX Catalog used to render every desktop-client template.

    JinjaX builds its own internal Jinja Environment but copies autoescape +
    filters from any seed env you pass in. We seed with the same autoescape
    config the old standalone JINJA_ENV used so user-controlled strings (form
    errors, agent IDs, etc.) stay HTML-escaped exactly as before.

    ``BTN_BASE`` / ``BTN_VARIANTS`` are exposed as Jinja globals so the
    three button components can share a single source of truth instead of
    each redeclaring the same class string + variants map.
    """
    seed_env = Environment(
        autoescape=select_autoescape(default_for_string=True, default=True),
    )
    catalog = Catalog(
        jinja_env=seed_env,
        globals={
            "BTN_BASE": _BTN_BASE,
            "BTN_SIZES": _BTN_SIZES,
            "BTN_VARIANTS": _BTN_VARIANTS,
            "INPUT_BASE": _INPUT_BASE,
            "ICONS_16": _ICONS_16,
            "ICONS_12": _ICONS_12,
            # Resolved per render so the page only boots the frontend Sentry SDK
            # when reporting is enabled (returns None otherwise). See
            # imbue/minds/utils/sentry/frontend.py and Base.jinja.
            "frontend_sentry_browser_payload": frontend_sentry_browser_payload,
        },
    )
    catalog.add_folder(str(TEMPLATE_DIR))
    return catalog


CATALOG: Final[Catalog] = _build_catalog()


# -- Page renderers --


@pure
def render_landing_page(
    accessible_agent_ids: Sequence[AgentId],
    mngr_forward_origin: str = "",
    telegram_status_by_agent_id: dict[str, bool] | None = None,
    is_discovering: bool = False,
    agent_names: dict[str, str] | None = None,
    destroying_status_by_agent_id: dict[str, str] | None = None,
    agent_accents: dict[str, str] | None = None,
    shutdown_capable_agent_ids: Sequence[AgentId] | None = None,
    mind_liveness_by_agent_id: dict[str, str] | None = None,
    agent_providers: dict[str, str] | None = None,
) -> str:
    """Render the landing page listing accessible workspaces.

    ``mngr_forward_origin`` is the bare origin of the ``mngr forward`` plugin
    (e.g. ``"http://localhost:8421"``). Workspace links target
    ``{mngr_forward_origin}/goto/<agent>/`` because Phase 2 deletes minds'
    in-process subdomain forwarder; the plugin owns ``/goto/`` now.

    telegram_status_by_agent_id maps agent ID strings to whether they have
    active Telegram bot credentials. When None, no telegram buttons are shown.

    agent_names maps agent ID strings to human-readable workspace names.

    agent_accents maps agent ID strings to ``#rrggbb`` workspace accent
    hexes (the stored color label, resolved by the caller). Agents without
    an entry -- including the whole map being None -- render their homepage
    tile with the default workspace color.

    destroying_status_by_agent_id maps agent ID strings to one of
    ``"running"``/``"failed"`` for agents whose detached destroy subprocess
    is currently in flight (running) or exited without removing the agent
    (failed). Agents whose destroy is ``done`` are not included -- the
    landing handler deletes those records so the row vanishes naturally
    once discovery propagates ``AgentDestroyed``. When None, no marker is
    shown.

    When is_discovering is True, the page shows a "Discovering agents..." message
    with auto-refresh instead of the empty state. This is used when the
    envelope-stream consumer hasn't completed initial agent discovery yet.
    """
    # Workspaces without an entry in agent_accents (caller didn't supply
    # one, or supplied a partial map) fall back to the default workspace
    # color so the homepage tile still paints with something readable.
    effective_accents: dict[str, str] = {}
    supplied = agent_accents or {}
    for aid in accessible_agent_ids:
        effective_accents[str(aid)] = supplied.get(str(aid), DEFAULT_WORKSPACE_COLOR)
    shutdown_capable_agent_id_strings = [str(aid) for aid in (shutdown_capable_agent_ids or ())]
    return CATALOG.render(
        "pages.Landing",
        agent_ids=accessible_agent_ids,
        agent_accents=effective_accents,
        mngr_forward_origin=mngr_forward_origin,
        telegram_enabled=telegram_status_by_agent_id is not None,
        telegram_status_by_agent_id=telegram_status_by_agent_id or {},
        is_discovering=is_discovering,
        agent_names=agent_names or {},
        destroying_status_by_agent_id=destroying_status_by_agent_id or {},
        shutdown_capable_agent_ids=shutdown_capable_agent_id_strings,
        mind_liveness_by_agent_id=mind_liveness_by_agent_id or {},
        agent_providers=agent_providers or {},
    )


# Hardcoded fallbacks for the workspace-creation form. Overridable via the
# MINDS_WORKSPACE_* env vars only when the operator explicitly opts in -- see
# ``_operator_workspace_default`` for the gating rationale.
_FALLBACK_GIT_URL: Final[str] = "https://github.com/imbue-ai/forever-claude-template.git"
_FALLBACK_HOST_NAME: Final[str] = "assistant"
# Pin to an annotated FCT tag so a shipped binary clones the exact FCT
# snapshot it was verified against. Bump to a newer tag only after
# re-verifying launch-to-msg CI against (this binary, the new tag).
FALLBACK_BRANCH: Final[str] = "minds-v0.3.3"

# Env var (set by ``just minds-start`` and the e2e workspace runner) that opts a
# launch into the operator's local-worktree create-form defaults. Gating on an
# explicit opt-in -- rather than on the tier -- means dev iteration works on ANY
# tier (including staging / production) when launched via ``just minds-start``,
# while a normal end-user ``minds run`` never honors a stray MINDS_WORKSPACE_*
# left over in the operator's shell, on any tier. The previous tier-based gate
# did the opposite: it blocked legitimate dev iteration on staging (forcing the
# form back to the public GitHub FCT on ``main``) while leaving dev tiers exposed
# to stray vars.
_WORKSPACE_DEFAULTS_OPT_IN_ENV_VAR: Final[str] = "MINDS_USE_LOCAL_WORKSPACE_DEFAULTS"


def _operator_workspace_default(env_var: str, fallback: str) -> str:
    """Return ``env_var`` only when the operator explicitly opted in; else ``fallback``.

    The MINDS_WORKSPACE_GIT_URL / _NAME / _BRANCH env vars wire the create-form
    defaults to the operator's local FCT worktree. They are honored only when
    ``MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1`` is set in the same environment
    (``just minds-start`` and the e2e runner set it). An end-user ``minds run``
    never sets it, so a stray MINDS_WORKSPACE_* left in the shell is ignored on
    every tier -- the safety the previous tier-based gate provided, without also
    blocking dev iteration on staging / production.

    These defaults point at a *local* path and a dev branch, which only make
    sense for local-compute launch modes (Lima / Docker). For IMBUE_CLOUD (pool
    lease) they must not be kept -- a pool host cannot clone a local path and the
    dev branch matches no pre-baked host -- so the opt-in is the operator's
    signal that they are doing local dev iteration, not an end-user pool create.
    """
    if os.environ.get(_WORKSPACE_DEFAULTS_OPT_IN_ENV_VAR) != "1":
        return fallback
    return os.environ.get(env_var, fallback)


@pure
def render_create_form(
    git_url: str = "",
    host_name: str = "",
    branch: str = "",
    launch_mode: LaunchMode | None = None,
    ai_provider: AIProvider | None = None,
    backup_provider: BackupProvider | None = None,
    backup_encryption_method: BackupEncryptionMethod | None = None,
    backup_api_key_env: str = "",
    has_saved_backup_password: bool = False,
    accounts: Sequence[object] | None = None,
    default_account_id: str = "",
    anthropic_api_key: str = "",
    error_message: str = "",
    region_options_by_launch_mode: Mapping[str, Sequence[str]] | None = None,
    region_selected_by_launch_mode: Mapping[str, str] | None = None,
    color: str = DEFAULT_WORKSPACE_COLOR,
) -> str:
    """Render the agent creation form page.

    The compute provider (``launch_mode``), AI provider, and backup provider
    are independent. The compute / AI providers default to ``IMBUE_CLOUD``
    when an account is selected; without an account they drop to ``LIMA`` /
    ``SUBSCRIPTION``. The backup provider defaults to ``IMBUE_CLOUD`` with an
    account and ``CONFIGURE_LATER`` without one. The backup encryption method
    defaults to ``NO_PASSWORD``.

    ``has_saved_backup_password`` toggles the master-password input between a
    "enter a passphrase" field (no saved password yet) and a read-only
    "a saved password will be used" indicator.

    ``host_name`` is the value of the form's "Name" field; it drives the
    host name on the resulting workspace. (The agent itself is always
    named ``system-services``.)

    ``color`` is the ``#rrggbb`` hex preselected in the form's palette
    picker: the matching swatch renders checked and the hidden ``color``
    input the form POSTs carries it. Callers pass the
    suggested-unused-palette pick; it defaults to
    ``DEFAULT_WORKSPACE_COLOR`` so callers that don't care about color
    (e.g. some tests) can omit it.
    """
    effective_url = git_url if git_url else _operator_workspace_default("MINDS_WORKSPACE_GIT_URL", _FALLBACK_GIT_URL)
    effective_name = (
        host_name if host_name else _operator_workspace_default("MINDS_WORKSPACE_NAME", _FALLBACK_HOST_NAME)
    )
    effective_branch = branch if branch else _operator_workspace_default("MINDS_WORKSPACE_BRANCH", FALLBACK_BRANCH)
    has_account = bool(default_account_id and accounts)
    effective_launch_mode = (
        launch_mode if launch_mode is not None else (LaunchMode.IMBUE_CLOUD if has_account else LaunchMode.LIMA)
    )
    effective_ai_provider = (
        ai_provider
        if ai_provider is not None
        else (AIProvider.IMBUE_CLOUD if has_account else AIProvider.SUBSCRIPTION)
    )
    effective_backup_provider = (
        backup_provider
        if backup_provider is not None
        else (BackupProvider.IMBUE_CLOUD if has_account else BackupProvider.CONFIGURE_LATER)
    )
    effective_backup_encryption = (
        backup_encryption_method if backup_encryption_method is not None else BackupEncryptionMethod.NO_PASSWORD
    )
    return CATALOG.render(
        "pages.Create",
        git_url=effective_url,
        host_name=effective_name,
        branch=effective_branch,
        launch_modes=list(LaunchMode),
        selected_launch_mode=effective_launch_mode.value,
        ai_providers=list(AIProvider),
        selected_ai_provider=effective_ai_provider.value,
        backup_providers=list(BackupProvider),
        selected_backup_provider=effective_backup_provider.value,
        backup_encryption_methods=list(BackupEncryptionMethod),
        selected_backup_encryption_method=effective_backup_encryption.value,
        backup_api_key_env=backup_api_key_env,
        has_saved_backup_password=has_saved_backup_password,
        accounts=accounts or [],
        default_account_id=default_account_id,
        anthropic_api_key=anthropic_api_key,
        error_message=error_message,
        region_options_by_launch_mode={
            key: list(value) for key, value in (region_options_by_launch_mode or {}).items()
        },
        region_selected_by_launch_mode=dict(region_selected_by_launch_mode or {}),
        color=color,
        palette=WORKSPACE_PALETTE,
    )


_STATUS_TEXT_DEFAULT: Final[dict[str, str]] = {
    "INITIALIZING": "Starting...",
    "CLONING_REPO": "Cloning repository...",
    "CHECKING_OUT_BRANCH": "Checking out branch...",
    "PROVISIONING_AI": "Provisioning AI access...",
    "CREATING_WORKSPACE": "Creating workspace...",
    "WAITING_FOR_READY": "Waiting for workspace to be ready...",
    "DONE": "Done. Redirecting...",
}

# IMBUE_CLOUD diverges in wording for the connection / agent-setup phases
# where the user-facing mental model is "connecting to / setting up an
# existing pool host" rather than "cloning / creating a new workspace".
_STATUS_TEXT_IMBUE_CLOUD: Final[dict[str, str]] = {
    "INITIALIZING": "Starting...",
    "CLONING_REPO": "Connecting to host...",
    "CHECKING_OUT_BRANCH": "Checking out branch...",
    "PROVISIONING_AI": "Provisioning AI access...",
    "CREATING_WORKSPACE": "Setting up agent...",
    "WAITING_FOR_READY": "Waiting for workspace to be ready...",
    "DONE": "Done. Redirecting...",
}


@pure
def status_text_for(
    status: str,
    error: str | None = None,
    launch_mode: LaunchMode = LaunchMode.DOCKER,
) -> str:
    """Resolve the UI caption for an ``AgentCreationStatus`` value.

    ``status`` is the stringified enum value (e.g. ``"CLONING_REPO"``).
    ``error`` is consulted only for the ``FAILED`` case so the caption
    can surface the underlying error message; for every other status the
    text comes from the mode-aware ``_STATUS_TEXT_*`` maps.
    """
    if status == "FAILED":
        return "Failed: {}".format(error or "unknown error")
    text_map = _STATUS_TEXT_IMBUE_CLOUD if launch_mode is LaunchMode.IMBUE_CLOUD else _STATUS_TEXT_DEFAULT
    return text_map.get(status, "Working...")


@pure
def render_creating_page(
    creation_id: CreationId,
    info: AgentCreationInfo,
) -> str:
    """Render the progress page shown while an agent is being created.

    The page is keyed by ``creation_id`` (minds-internal in-flight handle)
    rather than ``agent_id`` because the canonical agent id only comes
    into existence once the inner ``mngr create`` returns -- the page
    needs a stable handle to poll status from the moment the user kicks
    off the form. The template's status-poll URL still includes this id
    so SSE/log-streaming endpoints can find the right ``log_queue``.

    The launch mode is read off ``info.launch_mode`` --
    ``AgentCreator.start_creation`` records it before spawning the worker
    thread, so the ``AgentCreationInfo`` snapshot is the single source of
    truth for caption resolution (consistent with the SSE status events).
    """
    status_text = status_text_for(str(info.status), error=info.error, launch_mode=info.launch_mode)
    return CATALOG.render(
        "pages.Creating",
        agent_id=creation_id,
        status_text=status_text,
        # Drives the client-side time-based progress bar on the loading
        # screen (eases toward ~80% over this duration).
        expected_duration_seconds=expected_creation_duration_seconds(info.launch_mode),
    )


@pure
def render_welcome_page() -> str:
    """Render the welcome/splash page for first-time users."""
    return CATALOG.render("pages.Welcome")


@pure
def render_login_page() -> str:
    """Render the login prompt page for unauthenticated users."""
    return CATALOG.render("pages.Login")


@pure
def render_login_redirect_page(one_time_code: OneTimeCode) -> str:
    """Render the JS redirect page that forwards to /authenticate."""
    return CATALOG.render("pages.LoginRedirect", one_time_code=one_time_code)


@pure
def render_auth_error_page(message: str) -> str:
    """Render an error page for failed authentication."""
    return CATALOG.render("pages.AuthError", message=message)


@pure
def render_inbox_page(
    cards: Sequence[Mapping[str, str]],
    selected_id: str = "",
    detail_html: str = "",
    is_empty: bool = False,
    auto_open: bool = True,
) -> str:
    """Render the full inbox modal page served by ``GET /inbox``.

    ``cards`` is the initial left-list content (most-recent-first).
    ``selected_id`` highlights one card; ``detail_html`` is the
    pre-rendered right-pane fragment (handler detail, unavailable
    fragment, or empty). ``is_empty`` is True when there are no
    pending requests and the layout collapses to a centered message.
    ``auto_open`` is the initial state of the "Auto-open on new
    request" checkbox in the inbox header.
    """
    return CATALOG.render(
        "pages.Inbox",
        cards=cards,
        selected_id=selected_id,
        detail_html=detail_html,
        is_empty=is_empty,
        auto_open=auto_open,
    )


@pure
def render_inbox_list_fragment(
    cards: Sequence[Mapping[str, str]],
    selected_id: str = "",
) -> str:
    """Render the inbox left-list fragment served by ``GET /inbox/list``."""
    return CATALOG.render("InboxList", cards=cards, selected_id=selected_id)


@pure
def render_inbox_unavailable_fragment(message: str = "") -> str:
    """Render the inbox right-pane "no longer available" fragment.

    Returned by ``GET /inbox/detail/<id>`` when the id is unknown or
    already resolved; also innerHTML-swapped into the right pane by the
    inbox shell JS when an SSE event resolves the currently-selected
    item.

    ``message`` is an optional supporting sentence rendered under the
    fragment's heading. When empty (the default), only the heading is
    shown, so callers that drop the supporting sentence don't end up
    duplicating the heading.
    """
    return CATALOG.render("InboxUnavailable", message=message)


# CSS for the recovery page's restart controls, appended to the shared
# ``LOADING_PAGE_CSS``. The card itself, spinner, heading and message all come
# from the shared loading page, so the recovery page's loading state is
# byte-identical to the mngr_forward proxy loader.
_RECOVERY_STYLE: Final[str] = """\
      .hidden { display: none; }

      /* Keep the whole card within the viewport and lay it out as a vertical
         stack: the header row and the restart button stay pinned at the top,
         and only the troubleshooting block scrolls when its disclosures are
         expanded. Without this the card grows past the viewport as dropdowns
         open and -- because the body flex-centers it -- the heading and button
         slide off the top, out of reach of the page scrollbar. This overrides
         the shared ``.card`` from LOADING_PAGE_CSS (appended after it, so it
         wins); the proxy loader never pulls in this style, so it is unaffected.
         The 48px subtracted matches the body's 24px top+bottom padding. */
      .card {
        display: flex;
        flex-direction: column;
        max-height: calc(100vh - 48px);
      }
      .row { flex-shrink: 0; }

      /* Primary action. The restart and retry buttons are the page's focal
         point: full width, prominent, directly under the message. They are
         mutually exclusive (only one shows at a time, per the rendered tier)
         and share this styling. Most users only ever need this -- the
         troubleshooting disclosures below are for the rare deep-debugging
         case. */
      #recovery-host-btn,
      #recovery-retry-btn {
        margin-top: 20px;
        flex-shrink: 0;
        width: 100%;
        background: #18181b;
        color: #fff;
        border: 0;
        border-radius: 8px;
        padding: 12px 16px;
        font-size: 0.9375rem;
        font-weight: 600;
        cursor: pointer;
      }
      #recovery-host-btn:hover,
      #recovery-retry-btn:hover { background: #3f3f46; }
      #recovery-host-btn.secondary { background: #6b7280; }
      #recovery-host-btn.secondary:hover { background: #4b5563; }

      /* The verbatim provider error (e.g. docker's "Docker Desktop is manually
         paused..."), shown under the generic "may be temporarily unavailable"
         copy on the provider-unavailable tier. Set as plain text by the JS, so
         it carries whatever the provider returned -- a muted, left-bordered
         block keeps it visually distinct from our own copy. ``overflow-wrap``
         keeps a long unbroken token (e.g. the http+docker URL some messages
         embed) from overflowing the card. */
      .recovery-provider-reason {
        margin: 12px 0 0;
        padding: 8px 12px;
        border-left: 3px solid #e4e4e7;
        background: #fafafa;
        border-radius: 4px;
        color: #71717a;
        font-size: 0.8125rem;
        line-height: 1.4;
        text-align: left;
        overflow-wrap: anywhere;
      }

      /* Secondary, rarely-needed troubleshooting block: the error and
         diagnostics disclosures, grouped below a muted label and a thin
         divider. The whole block self-hides whenever neither disclosure is
         currently shown (both carry ``.hidden``), so the divider and label
         never appear over an empty section. */
      .recovery-troubleshooting {
        margin-top: 20px;
        padding-top: 16px;
        border-top: 1px solid #f4f4f5;
        /* The block can shrink below its content height (min-height: 0 frees
           it from the default flex min-content floor) and scrolls internally
           once the card hits its viewport cap, so expanding many disclosures
           never pushes the pinned header and button off-screen. */
        min-height: 0;
        overflow-y: auto;
      }
      .recovery-troubleshooting:not(:has(> details:not(.hidden))) { display: none; }
      .recovery-troubleshooting-label {
        font-size: 0.6875rem;
        font-weight: 600;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: #a1a1aa;
        margin: 0 0 6px;
      }
      .recovery-troubleshooting > details {
        margin: 0 0 8px;
        border: 1px solid #f4f4f5;
        background: #fff;
        border-radius: 8px;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
        color: #52525b;
      }
      .recovery-troubleshooting > details:last-child { margin-bottom: 0; }
      .recovery-troubleshooting > details > summary {
        display: flex;
        align-items: center;
        justify-content: space-between;
        cursor: pointer;
        padding: 9px 12px;
        font-weight: 500;
        font-size: 0.8125rem;
        color: #52525b;
        list-style: none;
      }
      .recovery-troubleshooting > details > summary::-webkit-details-marker { display: none; }
      .recovery-troubleshooting > details > summary::after {
        content: "\\25BE";
        color: #a1a1aa;
        font-size: 0.75rem;
        transition: transform 0.15s;
      }
      .recovery-troubleshooting > details[open] > summary::after { transform: rotate(180deg); }
      .recovery-troubleshooting > details > summary:hover { color: #3f3f46; }
      .recovery-troubleshooting > details[open] > summary { border-bottom: 1px solid #f4f4f5; }
      .recovery-troubleshooting > details > :not(summary) { padding: 10px 12px; }

      details pre {
        margin: 0;
        padding: 10px 12px;
        max-height: 240px;
        overflow-y: auto;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        font-size: 0.75rem;
        line-height: 1.5;
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        background: #fafafa;
        color: #3f3f46;
        border-radius: 6px;
      }
      .probe-row {
        margin: 4px 0 0;
        border: 1px solid #f4f4f5;
        background: #fff;
        border-radius: 6px;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
      }
      .probe-row summary {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 10px;
        font-size: 0.8125rem;
        font-weight: 500;
        cursor: pointer;
        color: #52525b;
        list-style: none;
      }
      .probe-row summary::-webkit-details-marker { display: none; }
      .probe-row summary::after {
        content: "\\25BE";
        color: #a1a1aa;
        font-size: 0.75rem;
        transition: transform 0.15s;
      }
      .probe-row[open] summary::after { transform: rotate(180deg); }
      .probe-row .probe-question { flex: 1; }
      .probe-glyph {
        display: inline-block;
        width: 1em;
        text-align: center;
        font-weight: 700;
      }
      .probe-glyph-yes { color: #047857; }
      .probe-glyph-no { color: #b91c1c; }
      .probe-glyph-unknown { color: #92400e; }
      #copy-diagnostics-btn,
      #copy-ssh-btn {
        margin-top: 8px;
        background: #fff;
        color: #52525b;
        border: 1px solid #d4d4d8;
        border-radius: 6px;
        font-size: 0.75rem;
        font-weight: 500;
        padding: 6px 12px;
        cursor: pointer;
      }
      #copy-ssh-btn { margin-left: 8px; }
      #copy-diagnostics-btn:hover,
      #copy-ssh-btn:hover { background: #f4f4f5; }
"""

# The recovery page's behavior. It drives the shared loading card (toggling
# the spinner, heading and message) plus the recovery-only restart button and
# error <details>. While a restart is in flight it auto-refreshes itself:
# _handle_recovery_page re-renders from the live tracker state on every GET,
# so a timed reload is the whole "is it healthy yet?" check.
_RECOVERY_SCRIPT: Final[str] = """\
      (function () {
        var root = document.querySelector('[data-agent-id]');
        if (!root) return;
        var agentId = root.dataset.agentId;
        var returnTo = root.dataset.returnTo || '';
        var initialStatus = root.dataset.initialStatus || 'stuck';

        var titleEl = document.getElementById('loading-title');
        var messageEl = document.getElementById('loading-message');
        var spinnerEl = document.getElementById('loading-spinner');
        var errorEl = document.getElementById('recovery-error');  // null unless restart_failed
        var hostBtn = document.getElementById('recovery-host-btn');
        // Shown (in place of the restart button) on the provider-unavailable and
        // workspace-unreachable states, where a restart cannot help; re-runs the
        // host-health probe so the user can re-check reachability on demand.
        var retryBtn = document.getElementById('recovery-retry-btn');
        // Holds the verbatim provider error on the backend-unreachable state;
        // hidden (and emptied) on every other state. Populated by
        // renderBackendUnreachable from the response's ``unreachable_reason``.
        var providerReasonEl = document.getElementById('recovery-provider-reason');
        var debugDetailsEl = document.getElementById('recovery-debug-details');
        var debugContentEl = document.getElementById('recovery-debug-content');
        var copyBtn = document.getElementById('copy-diagnostics-btn');
        // Present only for SSH-reachable hosts (every real workspace). Carries
        // the prebuilt connection command in its data attribute; absent (and so
        // null here) when the resolver has no SSH info for the agent.
        var copySshBtn = document.getElementById('copy-ssh-btn');

        var latestHealth = null;

        // A timed reload restarts the spinner's CSS animation from 0deg, so the
        // interval must be a whole multiple of the spinner's 1s rotation period
        // (see LOADING_PAGE_CSS' ``spin`` keyframe) -- otherwise the spinner
        // visibly jumps back mid-rotation on every refresh. 1000ms also matches
        // the mngr_forward proxy loader's 1s meta refresh, keeping the two
        // loading pages a user may see during recovery in lockstep.
        var REFRESH_INTERVAL_MS = 1000;

        function show(el, visible) {
          if (el) el.classList.toggle('hidden', !visible);
        }

        function escapeHtml(s) {
          if (s === null || s === undefined) return '';
          return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
        }

        function answerGlyph(answer) {
          if (answer === 'yes') return '<span class="probe-glyph probe-glyph-yes" aria-label="yes">&#x2713;</span>';
          if (answer === 'no') return '<span class="probe-glyph probe-glyph-no" aria-label="no">&#x2717;</span>';
          return '<span class="probe-glyph probe-glyph-unknown" aria-label="unknown">?</span>';
        }

        function renderDebugMenu(data) {
          if (!debugContentEl || !debugDetailsEl) return;
          if (!data || !Array.isArray(data.probes) || data.probes.length === 0) {
            debugContentEl.innerHTML = '';
            show(debugDetailsEl, false);
            return;
          }
          // Each probe is one row: glyph + question, with an expander
          // revealing the command that produced the answer and its raw output.
          var rows = data.probes.map(function (probe) {
            var glyph = answerGlyph(probe.answer);
            var body = '$ ' + probe.command + '\\n\\n' + probe.output;
            return '<details class="probe-row probe-row-' + escapeHtml(probe.answer || 'unknown') + '">'
              + '<summary>' + glyph + '<span class="probe-question">'
              + escapeHtml(probe.question) + '</span></summary>'
              + '<pre>' + escapeHtml(body) + '</pre>'
              + '</details>';
          });
          debugContentEl.innerHTML = rows.join('');
          show(debugDetailsEl, true);
        }

        function copyDiagnostics() {
          if (!latestHealth) return;
          try {
            var text = JSON.stringify(latestHealth, null, 2);
            if (navigator.clipboard) navigator.clipboard.writeText(text);
          } catch (e) {
            /* ignore */
          }
        }

        // The poll URL omits intent=restart so that, once the restart is
        // dispatched, a healthy tracker state 302s the user back to the workspace.
        function pollUrl() {
          var u = '/agents/' + encodeURIComponent(agentId) + '/recovery';
          if (returnTo) u += '?return_to=' + encodeURIComponent(returnTo);
          return u;
        }
        function scheduleRefresh() {
          setTimeout(function () { window.location.assign(pollUrl()); }, REFRESH_INTERVAL_MS);
        }
        // Background convergence poll for the restart_failed state. Unlike
        // scheduleRefresh (which reloads the whole page), this fetches pollUrl
        // with manual redirect handling: while the workspace is still down the
        // server returns the recovery HTML (200), which we discard so the
        // displayed failure reason + diagnostics stay put and the heavy
        // host-health probe is not re-run. Once the background probe loop flips
        // the tracker to HEALTHY the server starts 302ing to return_to, which
        // surfaces as an opaque-redirect response; we then follow it to send
        // the user back to the now-recovered workspace.
        function scheduleHealthyPoll() {
          setTimeout(function () {
            fetch(pollUrl(), { credentials: 'same-origin', redirect: 'manual' }).then(function (resp) {
              if (resp.type === 'opaqueredirect' || (resp.status >= 300 && resp.status < 400)) {
                window.location.assign(pollUrl());
                return;
              }
              scheduleHealthyPoll();
            }, function () {
              scheduleHealthyPoll();
            });
          }, REFRESH_INTERVAL_MS);
        }


        function renderLoading() {
          titleEl.textContent = 'Loading workspace';
          messageEl.textContent = '';
          show(spinnerEl, true);
          show(errorEl, false);
          show(hostBtn, false);
          show(retryBtn, false);
          // A stale diagnostic from the previous tick would be misleading
          // while we're in flight to a fresh check; hide it and drop the
          // cached payload so renderDebugMenu starts blank next time.
          show(debugDetailsEl, false);
          if (debugContentEl) debugContentEl.innerHTML = '';
          // Drop any prior provider error so it never lingers into the next state.
          if (providerReasonEl) { providerReasonEl.textContent = ''; show(providerReasonEl, false); }
          latestHealth = null;
        }
        // The shared "Workspace unresponsive" state -- shown for ambiguous-host
        // states, after a restart failure, and whenever the container is live
        // but unreachable (bouncing it would interrupt user agents, so we want
        // explicit consent before doing so).
        function renderUnresponsive() {
          titleEl.textContent = 'Workspace unresponsive';
          messageEl.textContent =
            'This workspace needs a restart to recover. In-progress work in all agents will be '
            + 'interrupted. If the problem persists, contact support.';
          show(spinnerEl, false);
          show(errorEl, true);
          hostBtn.textContent = 'Restart workspace';
          hostBtn.classList.remove('secondary');
          show(hostBtn, true);
        }
        function renderDispatchError() {
          titleEl.textContent = 'Workspace unresponsive';
          messageEl.textContent = 'Could not start the restart. Check your connection and try again.';
          show(spinnerEl, false);
          show(errorEl, false);
          hostBtn.textContent = 'Restart workspace';
          hostBtn.classList.remove('secondary');
          show(hostBtn, true);
        }
        // The provider/backend hosting this workspace is unreachable or rejected
        // us (connector down, docker daemon stopped or paused, expired login,
        // ...). A restart routes through that same backend, so it can't help --
        // offer only a Retry. The background healthy-poll (armed by applyHealth)
        // auto-returns the user to the workspace the moment it recovers and the
        // tracker flips HEALTHY. The copy is deliberately provider-agnostic (no
        // "check your internet" -- a local docker daemon is independent of the
        // network); the actual cause comes from the provider itself via
        // ``unreachable_reason``, surfaced verbatim below so we never have to
        // hand-author a message per provider.
        function renderBackendUnreachable(data) {
          var label = (data && data.provider_label) || 'the workspace backend';
          var reason = (data && data.unreachable_reason) || '';
          titleEl.textContent = "Can't connect to " + label;
          messageEl.textContent =
            label + ' may be temporarily unavailable. This page will reconnect '
            + 'automatically once it can reach your workspace again.';
          if (providerReasonEl) {
            providerReasonEl.textContent = reason;
            show(providerReasonEl, Boolean(reason));
          }
          show(spinnerEl, false);
          show(errorEl, false);
          show(hostBtn, false);
          show(retryBtn, true);
          // No diagnostics here: when the backend itself is unreachable the
          // in-container probes are moot -- the cause is the provider's own
          // error, shown verbatim above -- so suppress the Diagnostics disclosure.
          show(debugDetailsEl, false);
        }

        function postRestart(path) {
          renderLoading();
          // The endpoint returns 202 once the tracker is RESTARTING; any other
          // status means the dispatch did not start, so surface an error
          // instead of refreshing into a re-probe loop.
          fetch('/api/agents/' + encodeURIComponent(agentId) + path, {
            method: 'POST',
            credentials: 'same-origin',
          }).then(function (resp) {
            if (resp.ok) { scheduleRefresh(); } else { renderDispatchError(); }
          }, renderDispatchError);
        }

        function fetchHealth() {
          return fetch('/api/agents/' + encodeURIComponent(agentId) + '/host-health', {
            credentials: 'same-origin',
          }).then(function (resp) { return resp.json(); });
        }

        // Render (and, when ``autoDispatch``, dispatch a restart for) the tier in
        // a host-health payload. The recovery page is only reached once discovery
        // is fresh (the redirect is gated on freshness), so the classification is
        // trustworthy and there is no transient awaiting-discovery state to
        // converge through.
        function applyHealth(data, autoDispatch) {
          latestHealth = data || null;
          renderDebugMenu(latestHealth);
          var tier = data && data.dispatch_tier;
          // A backend-unreachable outcome short-circuits before any restart
          // dispatch on EVERY entry path: no restart can or should fire while the
          // backend is unreachable or rejecting us. Render-only, and arm the
          // background healthy-poll so the page auto-returns once the backend
          // recovers (a resumed daemon and a restored login recover identically).
          if (tier === 'backend_unreachable') {
            renderBackendUnreachable(data);
            scheduleHealthyPoll();
            return;
          }
          if (!autoDispatch) {
            // restart_failed entry: render unresponsive so the failure reason and
            // the diagnostics list both stay visible.
            renderUnresponsive();
            return;
          }
          if (tier === 'host_offline') {
            // Container fully stopped: nothing live to interrupt, dispatch
            // unattended. Tell the endpoint the host is already stopped so it
            // skips the redundant stop step and cold-boots straight away.
            postRestart('/restart-host?host_already_stopped=1');
            return;
          }
          if (tier === 'interface_unresponsive') {
            // Container running, exec works: restart the system-services agent in place.
            postRestart('/restart-system-interface');
            return;
          }
          // 'host_unresponsive' or anything else: require explicit user consent for a host restart.
          renderUnresponsive();
        }

        // Fetch the host-health probe and populate the diagnostic. When
        // ``autoDispatch`` is true (the live stuck/probe entry) we also pick
        // a restart tier from ``dispatch_tier``; when it's false (the
        // restart_failed entry) we only render the diagnostic alongside the
        // existing failure-reason error block, so the user sees both.
        function runProbe(autoDispatch) {
          renderLoading();
          fetchHealth().then(function (data) {
            applyHealth(data, autoDispatch);
          }, function () {
            renderUnresponsive();
          });
        }

        hostBtn.addEventListener('click', function () {
          postRestart('/restart-host');
        });
        if (retryBtn) {
          retryBtn.addEventListener('click', function () {
            // Re-check reachability immediately. autoDispatch stays true, but the
            // provider/unreachable tiers are render-only, so this never dispatches
            // a restart -- it just refreshes the probe and re-renders the state.
            runProbe(true);
          });
        }
        if (copyBtn) {
          copyBtn.addEventListener('click', copyDiagnostics);
        }
        if (copySshBtn) {
          copySshBtn.addEventListener('click', function () {
            var cmd = copySshBtn.getAttribute('data-ssh-command') || '';
            try {
              if (navigator.clipboard) navigator.clipboard.writeText(cmd);
            } catch (e) {
              /* ignore */
            }
          });
        }

        if (initialStatus === 'restarting') {
          renderLoading();
          scheduleRefresh();
        } else if (initialStatus === 'restart_failed') {
          // Show the failure reason AND the diagnostic together: re-run
          // the probe with auto-dispatch off so the renderUnresponsive path
          // also has the diagnostics populated.
          runProbe(false);
          // A failed restart is not necessarily terminal: the background probe
          // loop keeps polling the workspace and may recover it on its own
          // (e.g. a cold container boot that finished just after the restart
          // worker's bounded wait elapsed). Watch for that recovery so we can
          // return the user to the workspace without them having to act.
          scheduleHealthyPoll();
        } else if (initialStatus === 'healthy') {
          // Degenerate: rendered HEALTHY with no return_to to 302 to. Offer a
          // manual restart rather than auto-dispatching one on a healthy page.
          renderUnresponsive();
        } else {
          runProbe(true);
        }
      })();
"""


@pure
def render_recovery_page(
    agent_id: AgentId,
    return_to: str,
    initial_status: str,
    initial_error: str,
    ssh_command: str | None = None,
) -> str:
    """Render the workspace-recovery page shown when the system interface is unresponsive.

    Built on the shared ``render_loading_page`` so the recovery page's loading
    state is identical to the mngr_forward proxy loader. ``initial_status`` is
    one of ``"stuck"``/``"restarting"``/``"restart_failed"``/``"healthy"`` and
    governs the page's initial UI state. ``initial_error`` is the failure
    reason shown (collapsed) when ``initial_status`` is ``"restart_failed"``.
    ``return_to`` is the URL the page navigates back to once the workspace is
    healthy again.

    ``ssh_command`` is the copy-pasteable SSH command for the agent's host. When
    provided, a "Copy SSH command" button sits beside "Copy diagnostics" in the
    Diagnostics menu; when ``None`` (no SSH info -- e.g. the brief window before
    discovery surfaces it) the button is omitted entirely rather than rendered
    inert.
    """
    error_block = ""
    if initial_error:
        error_block = (
            '        <details id="recovery-error" class="hidden">\n'
            "          <summary>Error details</summary>\n"
            f"          <pre>{html.escape(initial_error)}</pre>\n"
            "        </details>\n"
        )
    # Debug details are populated dynamically by the recovery JS once it gets
    # a host-health response. The block is in the DOM from the start (hidden)
    # so the JS can fill it in place without re-templating.
    ssh_button = ""
    if ssh_command is not None:
        ssh_button = (
            '<button type="button" id="copy-ssh-btn" '
            f'data-ssh-command="{html.escape(ssh_command, quote=True)}">Copy SSH command</button>'
        )
    debug_block = (
        '        <details id="recovery-debug-details" class="hidden">\n'
        "          <summary>Diagnostics</summary>\n"
        '          <div id="recovery-debug-content"></div>\n'
        '          <div class="debug-section">'
        '<button type="button" id="copy-diagnostics-btn">Copy diagnostics</button>'
        f"{ssh_button}"
        "</div>\n"
        "        </details>\n"
    )
    # The restart button is the page's primary action, so it comes first --
    # directly under the message. The error and diagnostics disclosures are
    # grouped together below it in the de-emphasized troubleshooting block;
    # ``_RECOVERY_STYLE`` self-hides that block (divider + label included)
    # whenever neither disclosure is currently visible.
    card_extra = (
        '      <p id="recovery-provider-reason" class="recovery-provider-reason hidden"></p>\n'
        '      <button id="recovery-host-btn" class="hidden">Restart workspace</button>\n'
        '      <button id="recovery-retry-btn" class="hidden">Retry</button>\n'
        '      <div class="recovery-troubleshooting">\n'
        '        <p class="recovery-troubleshooting-label">Troubleshooting</p>\n'
        + error_block
        + debug_block
        + "      </div>\n"
    )
    card_attrs = (
        f' data-agent-id="{html.escape(str(agent_id))}"'
        f' data-return-to="{html.escape(return_to)}"'
        f' data-initial-status="{html.escape(initial_status)}"'
    )
    return render_loading_page(
        style_extra=_RECOVERY_STYLE,
        card_attrs=card_attrs,
        card_extra=card_extra,
        body_extra="    <script>\n" + _RECOVERY_SCRIPT + "    </script>\n",
    )


@pure
def render_destroying_page(
    agent_id: AgentId,
    agent_name: str,
    pid: int,
    status: str,
) -> str:
    """Render the detail page for an in-flight or recently-completed destroy.

    The page polls ``/api/destroying/<agent_id>/{status,log}`` to keep its
    log tail and status badge up to date; once status flips to ``done`` it
    redirects to ``/``. ``status`` is the initial server-side computed
    value (``running``/``failed``/``done``) so the page renders correctly
    even before the first poll completes.
    """
    return CATALOG.render(
        "pages.Destroying",
        agent_id=str(agent_id),
        agent_name=agent_name,
        pid=pid,
        status=status,
    )


# -- Chrome (persistent shell) templates --


@pure
def render_chrome_page(
    is_mac: bool = False,
    is_authenticated: bool = False,
    mngr_forward_origin: str = "",
    initial_workspaces: Sequence[dict[str, str]] | None = None,
) -> str:
    """Render the persistent chrome page (title bar + sidebar + content iframe).

    is_mac controls whether macOS-specific styling is applied (traffic light padding,
    hidden window controls).

    ``mngr_forward_origin`` is exposed to the page-level JS via a
    ``data-mngr-forward-origin`` attribute on the body so chrome.js can build
    workspace links that target the plugin's port directly.

    In Electron mode, the iframe and browser sidebar are hidden via JS; the content
    is handled by a separate WebContentsView, and the sidebar page is loaded into
    the shared modal WebContentsView when opened.
    """
    return CATALOG.render(
        "pages.Chrome",
        is_mac=is_mac,
        is_authenticated=is_authenticated,
        mngr_forward_origin=mngr_forward_origin,
        initial_workspaces=initial_workspaces or [],
    )


@pure
def render_sidebar_page(
    mngr_forward_origin: str = "",
    trigger_x: int = 0,
    trigger_y: int = 0,
    trigger_w: int = 0,
    trigger_h: int = 38,
    offset_x: int = -2,
    offset_y: int = 2,
) -> str:
    """Render the standalone sidebar page loaded into the shared modal WebContentsView.

    This page shows the workspace list and subscribes to SSE updates. In Electron,
    clicking a workspace sends an IPC message via the preload bridge to navigate
    the content WebContentsView. ``mngr_forward_origin`` is exposed via
    ``data-mngr-forward-origin`` so sidebar.js can build the cross-origin
    ``/goto/<agent>/`` URL the plugin serves.

    Position is driven entirely by the caller. The chrome view (which owns the
    trigger button) passes the button's viewport-relative rect (``trigger_x``,
    ``trigger_y``, ``trigger_w``, ``trigger_h``) plus a caller-chosen offset
    (``offset_x``, ``offset_y``). The menu's top-left lands at the trigger's
    bottom-left + offset. The chrome view and the modal view share window
    coordinate space, so the rect translates directly. Defaults (no query
    params) anchor a 38px-tall element at the top-left of the window,
    nudged 2px left and 2px below it -- right for the titlebar's first button.
    """
    return CATALOG.render(
        "pages.Sidebar",
        mngr_forward_origin=mngr_forward_origin,
        trigger_x=trigger_x,
        trigger_y=trigger_y,
        trigger_w=trigger_w,
        trigger_h=trigger_h,
        offset_x=offset_x,
        offset_y=offset_y,
    )


# -- Workspace/settings/sharing/accounts --


@pure
def render_sharing_editor(
    agent_id: str,
    service_name: str,
    title: str,
    mngr_forward_origin: str = "",
    initial_emails: list[str] | None = None,
    has_account: bool = True,
    accounts: Sequence[object] | None = None,
    redirect_url: str = "",
    ws_name: str = "",
    account_email: str = "",
) -> str:
    """Render the sharing editor page used by the workspace-settings sharing flow.

    ``mngr_forward_origin`` is the bare origin of the ``mngr forward`` plugin;
    the workspace link in the page title points at ``{mngr_forward_origin}/goto/<agent>/``.
    """
    return CATALOG.render(
        "pages.Sharing",
        title=title,
        agent_id=agent_id,
        service_name=service_name,
        mngr_forward_origin=mngr_forward_origin,
        initial_emails=initial_emails or [],
        has_account=has_account,
        accounts=accounts or [],
        redirect_url=redirect_url,
        ws_name=ws_name,
        account_email=account_email,
    )


@pure
def render_workspace_settings(
    agent_id: str,
    ws_name: str,
    current_account: object | None,
    accounts: Sequence[object],
    servers: Sequence[str],
    telegram_state: str | None = None,
    is_leased_imbue_cloud: bool = False,
    current_color: str = DEFAULT_WORKSPACE_COLOR,
    is_stale: bool = False,
) -> str:
    """Render the workspace settings page.

    telegram_state controls whether the Telegram section is shown:

    - ``None`` -- no Telegram orchestrator configured; section is hidden.
    - ``"active"`` -- Telegram is already set up for this workspace.
    - ``"pending"`` -- setup button is shown.

    ``is_leased_imbue_cloud`` is True for workspaces on a host leased from
    Imbue Cloud; the account section then shows the bound account with a
    disabled Disassociate control and no association controls.

    ``current_color`` is the workspace's stored color hex (``#rrggbb``),
    used to pre-select a palette swatch / pre-fill the hex input.
    Defaults to ``DEFAULT_WORKSPACE_COLOR`` so callers that don't care
    about color (e.g. some tests) can omit it.

    ``is_stale`` reflects the workspace's provider-health flag from the
    SSE workspace payload; when True the color picker controls are
    disabled with a hint that the workspace is currently unreachable.

    Interactivity for the setup flow lives in ``static/workspace_settings.js``,
    which reads the agent id from the page's ``data-agent-id`` attribute.
    """
    return CATALOG.render(
        "pages.WorkspaceSettings",
        agent_id=agent_id,
        ws_name=ws_name,
        current_account=current_account,
        accounts=accounts,
        servers=servers,
        telegram_state=telegram_state,
        is_leased_imbue_cloud=is_leased_imbue_cloud,
        current_color=current_color,
        is_stale=is_stale,
        palette=WORKSPACE_PALETTE,
    )


# -- Dev styleguide --


@pure
def render_dev_styleguide_page() -> str:
    """Render the styleguide page (mounted at ``/_dev/styleguide``).

    The page is a hand-authored catalog of UI patterns and tokens. When a
    new ``:root`` token is added to ``static/app.css``, add a swatch
    in ``templates/pages/DevStyleguide.jinja`` with
    ``data-token="--<name>"`` on its wrapper -- the ``templates_test.py``
    ratchet cross-checks the set of declared ``:root`` tokens against the
    set of ``data-token`` swatches and fails if either side drifts.
    """
    return CATALOG.render("pages.DevStyleguide")


@pure
def render_accounts_page(
    accounts: Sequence[object],
    default_account_id: str | None = None,
    enabled_by_user_id: Mapping[str, bool] | None = None,
) -> str:
    """Render the manage accounts page.

    ``enabled_by_user_id`` maps each account's user_id to whether its
    ``[providers.imbue_cloud_<slug>]`` block is enabled in settings.toml.
    The template renders a "Signed out" indicator when an account is
    present (still in sessions.json) but the user disabled the block
    via the providers panel.
    """
    return CATALOG.render(
        "pages.Accounts",
        accounts=accounts,
        default_account_id=default_account_id or "",
        enabled_by_user_id=dict(enabled_by_user_id or {}),
    )
