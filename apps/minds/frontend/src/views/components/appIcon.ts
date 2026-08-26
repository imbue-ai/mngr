/**
 * A workspace app's own icon, made safe to inline in the shell, and the
 * monogram fallback for apps without one.
 *
 * Apps register their icon as SVG markup inside the workspace; it reaches the
 * shell through the services event log and the options endpoint. That markup
 * is authored by untrusted workspace content headed for the TRUSTED shell
 * DOM, so everything that draws one goes through `sanitizeIconMarkup` here.
 *
 * This is a port of the workspace's own gate (`appIcon.ts` in
 * default-workspace-template's system_interface, which documents each rule in
 * full) -- the two repos share no package, so the rules are mirrored by hand;
 * update both together. The gate in brief: DOMPurify parses with the
 * browser's own parser against an SVG allowlist; anything that can execute,
 * navigate, load, embed, or animate is refused (script, style, a, image,
 * iframe, foreignObject, the animation elements, `on*` handlers,
 * `style`/`class` attributes, and any URI that is not a `#fragment` into the
 * icon itself); the result must be exactly one `<svg>` element; ids are
 * namespaced per icon so two apps' gradients cannot collide document-wide.
 */

import DOMPurify from "dompurify";

const SVG_NAMESPACE = "http://www.w3.org/2000/svg";

// The same cap the workspace registry enforces on the way in.
export const MAX_ICON_LENGTH = 16384;

const FORBIDDEN_TAGS: readonly string[] = [
  "script",
  "style",
  "a",
  "image",
  "iframe",
  "foreignobject",
  "animate",
  "animatemotion",
  "animatetransform",
  "set",
  "handler",
];

// `style` because CSS can fetch; `class` because the shell's stylesheet is
// not the icon's to borrow.
const FORBIDDEN_ATTRIBUTES: readonly string[] = ["style", "class"];

// Attributes that name a resource: only a same-document `#fragment` is kept.
const REFERENCE_ATTRIBUTES: ReadonlySet<string> = new Set(["href", "xlink:href", "src", "xml:base"]);

// DOMPurify's stock URI expression with the schemes taken out: any scheme at
// all is refused, a scheme-less value or `#fragment` is kept.
const SCHEMELESS_OR_FRAGMENT_URI = /^(?:#|[^a-z]|[a-z+.-]+(?:[^a-z+.:-]|$))/i;

// What THIS module accepts in a resource-naming attribute: a fragment into
// the icon and nothing else.
const FRAGMENT_ONLY_URI = /^#[\w.:-]*$/;

// Checked on the raw string before parsing: sanitizing `<div><svg/></div>`
// would leave a drawable svg the author did not write.
const SVG_OPENING_TAG = /^<svg[\s>]/i;

// A `url(...)` paint/filter reference pointing anywhere but into this icon.
const EXTERNAL_URL_REFERENCE = /url\(\s*['"]?(?!#)/i;

const JAVASCRIPT_URI = /javascript:/i;

const MAX_CACHE_ENTRIES = 64;
const sanitizedByKey = new Map<string, string | null>();

/** Drop the whitespace and control characters a URL parser ignores, so an
 *  obfuscated scheme (a `javascript:` with a newline inside) is compared in
 *  its plain spelling. */
function collapsed(value: string): string {
  let result = "";
  for (const character of value) {
    if (character.charCodeAt(0) > 0x20) result += character;
  }
  return result;
}

/** FNV-1a, as an id prefix that is stable per icon (ids are document-wide,
 *  and two apps may both ship a gradient called "a"). */
function iconIdPrefix(markup: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < markup.length; index += 1) {
    hash ^= markup.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return `app-icon-${hash.toString(36)}-`;
}

/** The one element child of `fragment`, or null unless it is exactly one
 *  element with nothing beside it. */
function onlyElementChild(fragment: DocumentFragment): Element | null {
  const children = Array.from(fragment.childNodes);
  const elements = children.filter((node): node is Element => node.nodeType === Node.ELEMENT_NODE);
  if (elements.length !== 1) return null;
  const hasStrayText = children.some((node) => node.nodeType === Node.TEXT_NODE && node.textContent?.trim() !== "");
  return hasStrayText ? null : elements[0];
}

/** Drop every attribute that can execute or reach off the page. DOMPurify was
 *  asked for all of this through its config; this runs anyway so the rules
 *  are read at the DOM they apply to. */
function scrubAttributes(root: Element): void {
  for (const element of [root, ...Array.from(root.querySelectorAll("*"))]) {
    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.name.toLowerCase();
      const value = collapsed(attribute.value);
      const isUnsafe =
        name.startsWith("on") ||
        FORBIDDEN_ATTRIBUTES.includes(name) ||
        JAVASCRIPT_URI.test(value) ||
        EXTERNAL_URL_REFERENCE.test(value) ||
        (REFERENCE_ATTRIBUTES.has(name) && !FRAGMENT_ONLY_URI.test(value));
      if (isUnsafe) element.removeAttribute(attribute.name);
    }
  }
}

/** Rewrite every id in the icon, and every reference to one, behind `prefix`. */
function namespaceIds(root: Element, prefix: string): void {
  const elements = [root, ...Array.from(root.querySelectorAll("*"))];
  const renamed = new Map<string, string>();
  for (const element of elements) {
    const id = element.getAttribute("id");
    if (id !== null && id !== "") renamed.set(id, `${prefix}${id}`);
  }
  if (renamed.size === 0) return;
  for (const element of elements) {
    for (const attribute of Array.from(element.attributes)) {
      if (attribute.name.toLowerCase() === "id") {
        const rename = renamed.get(attribute.value);
        if (rename !== undefined) element.setAttribute(attribute.name, rename);
        continue;
      }
      let value = attribute.value;
      for (const [id, rename] of renamed) {
        if (value === `#${id}`) value = `#${rename}`;
        value = value.split(`url(#${id})`).join(`url(#${rename})`);
      }
      if (value !== attribute.value) element.setAttribute(attribute.name, value);
    }
  }
}

/** Numeric length attribute (`24`, `24px`), or null when it says something
 *  this cannot turn into a viewBox. */
function lengthAttribute(root: Element, name: string): number | null {
  const raw = root.getAttribute(name);
  if (raw === null) return null;
  const parsed = Number.parseFloat(raw);
  if (!Number.isFinite(parsed) || parsed <= 0) return null;
  return parsed;
}

/** Put the icon on the caller's grid: `sizePx` square, scaling rather than
 *  cropping (its viewBox is kept, or derived from its width/height), and
 *  inheriting `currentColor` unless it paints itself. */
function normalizeRoot(root: Element, sizePx: number): boolean {
  if (root.getAttribute("viewBox") === null) {
    const width = lengthAttribute(root, "width");
    const height = lengthAttribute(root, "height");
    if (width === null || height === null) return false;
    root.setAttribute("viewBox", `0 0 ${width} ${height}`);
  }
  root.setAttribute("width", String(sizePx));
  root.setAttribute("height", String(sizePx));
  root.setAttribute("aria-hidden", "true");
  root.setAttribute("focusable", "false");
  if (root.getAttribute("fill") === null) root.setAttribute("fill", "currentColor");
  return true;
}

/**
 * Registry icon markup, made safe to inline at `sizePx`, or null when it is
 * not a usable icon.
 *
 * Null is the answer for anything at all doubtful -- unparseable markup,
 * markup not rooted at a single `<svg>` element, art with no size to scale
 * from, markup over MAX_ICON_LENGTH, or a page with no DOM to sanitize
 * against. Callers draw their fallback on null, so refusing an icon costs a
 * picture rather than a surface.
 */
export function sanitizeIconMarkup(rawMarkup: string, sizePx: number): string | null {
  const markup = rawMarkup.trim();
  if (markup === "" || markup.length > MAX_ICON_LENGTH) return null;
  const key = `${sizePx}|${markup}`;
  const cached = sanitizedByKey.get(key);
  if (cached !== undefined) return cached;
  const sanitized = sanitizeUncached(markup, sizePx);
  if (sanitizedByKey.size >= MAX_CACHE_ENTRIES) sanitizedByKey.clear();
  sanitizedByKey.set(key, sanitized);
  return sanitized;
}

function sanitizeUncached(markup: string, sizePx: number): string | null {
  if (!SVG_OPENING_TAG.test(markup)) return null;
  // No DOM means no parser, and there is no safe way to inline unparsed
  // markup (the unit-test-under-node case; the browser always has one).
  if (!DOMPurify.isSupported) return null;
  const fragment = DOMPurify.sanitize(markup, {
    USE_PROFILES: { svg: true, svgFilters: true },
    FORBID_TAGS: [...FORBIDDEN_TAGS],
    FORBID_ATTR: [...FORBIDDEN_ATTRIBUTES],
    ALLOWED_URI_REGEXP: SCHEMELESS_OR_FRAGMENT_URI,
    RETURN_DOM_FRAGMENT: true,
  });
  const root = onlyElementChild(fragment);
  if (root === null) return null;
  if (root.namespaceURI !== SVG_NAMESPACE || root.tagName.toLowerCase() !== "svg") return null;
  scrubAttributes(root);
  namespaceIds(root, iconIdPrefix(markup));
  if (!normalizeRoot(root, sizePx)) return null;
  return root.outerHTML;
}

/**
 * What an app wears when it has registered no usable icon: its initial in an
 * outlined tile, in the house icon style (currentColor strokes), so each app
 * at least differs from its neighbours and matches how the workspace itself
 * draws it.
 */
export function appMonogramMarkup(appName: string, sizePx: number): string {
  // App names are agent/user text, so the letter is escaped before it lands
  // in markup that callers hand to `m.trust`.
  const initial = appName
    .trim()
    .charAt(0)
    .toUpperCase()
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="${sizePx}" height="${sizePx}" viewBox="0 0 24 24" ` +
    `fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ` +
    `aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="4"/>` +
    `<text x="12" y="12.7" stroke="none" fill="currentColor" font-size="11" font-weight="600" ` +
    `text-anchor="middle" dominant-baseline="central">${initial}</text></svg>`
  );
}

/** The markup a share target wears: its registered icon when usable, else its
 *  monogram -- the same chain the workspace draws it with. */
export function shareTargetIconMarkup(rawIcon: string, serviceName: string, sizePx: number): string {
  const sanitized = rawIcon === "" ? null : sanitizeIconMarkup(rawIcon, sizePx);
  return sanitized ?? appMonogramMarkup(serviceName, sizePx);
}
