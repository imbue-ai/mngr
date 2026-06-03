import { Show, splitProps } from 'solid-js';

// Icon-only button used inside dense rows (the gear / restart controls on
// the landing page). The visual treatment is the muted "ghost" style: no
// background until hover, no border, square padding.
//
// Two ways to provide the icon:
//   * pass an SVG via ``children`` (full control of stroke / path)
//   * pass a ``kind`` prop and we render the matching built-in SVG.
//
// Built-in kinds:
//   * ``settings`` -- 24x24 gear (mirrors landing.html's existing markup)
//   * ``restart`` -- 24x24 curved arrow (mirrors landing.html)

const BASE =
  'bg-transparent border border-transparent rounded-md cursor-pointer p-1.5 ' +
  'text-zinc-400 hover:bg-zinc-100 hover:text-zinc-600 flex items-center justify-center';

const SVG_PROPS = {
  class: 'w-4 h-4',
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  'stroke-width': '2',
  'stroke-linecap': 'round',
  'stroke-linejoin': 'round',
};

function SettingsIcon() {
  return (
    <svg {...SVG_PROPS}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}

function RestartIcon() {
  return (
    <svg {...SVG_PROPS}>
      <path d="M21 12a9 9 0 1 1-2.64-6.36" />
      <path d="M21 3v6h-6" />
    </svg>
  );
}

const ICON_BY_KIND = {
  settings: SettingsIcon,
  restart: RestartIcon,
};

export function IconButton(props) {
  const [local, rest] = splitProps(props, [
    'kind',
    'children',
    'class',
    'extra',
    'label',
  ]);
  const Icon = local.kind ? ICON_BY_KIND[local.kind] : null;
  if (local.kind && !Icon) {
    throw new Error(`Unknown icon kind: ${local.kind}`);
  }
  const cls = [BASE, local.class, local.extra].filter(Boolean).join(' ');
  // ``title`` and ``aria-label`` default to the same string for accessibility.
  return (
    <button
      type="button"
      class={cls}
      title={local.label || undefined}
      aria-label={local.label || undefined}
      {...rest}
    >
      <Show when={Icon} fallback={local.children}>
        <Icon />
      </Show>
    </button>
  );
}
