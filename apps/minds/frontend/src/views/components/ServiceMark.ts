// A service's brand mark: the vendor's own logo, shared by every surface that
// names a service -- the Permissions tab's left nav, connection headings, Add
// connection and the Waiting strip, and the request-review popup's header.

import m from "mithril";

/** How a mark is drawn. A connection's own entries show the logo exactly as
 * the vendor publishes it while the account is connected, and a drained,
 * dimmed copy of that same logo once it is not. */
export type MarkTone = "brand" | "muted";

/** Services whose logo is near-black and disappears on the dark theme's
 * surface, and that the vendor publishes a white variant of. Nothing here is
 * recolored: the variant is the brand's own artwork, shipped beside the first
 * as `<service>-on-dark.svg`. A brand with no published white variant is
 * absent, and keeps its single mark in both themes.
 *
 * Exported so ServiceMark.test.ts can pin it: the variant `<img>` carries no
 * `onerror` probe (a missing variant must not retire a base logo that loads),
 * so a name added here without its file would show as a broken image with no
 * fallback. That test pins this set to a literal, and service_icons_test.py
 * pins the same literal to the files on disk. */
export const DARK_SURFACE_VARIANT_SERVICE_NAMES: ReadonlySet<string> = new Set([
  "aws",
  "github",
  "linear",
  "ngrok",
  "ramp",
  "sentry",
  "umami",
]);

/** Services whose mark asset 404'd. App-global rather than per-view: which
 * marks exist on disk is a property of the server, not of the surface asking,
 * so one surface's failed load spares every other surface the same 404. */
const markFailedServiceNames = new Set<string>();

/** Forget which marks 404'd. For tests only: the set is app-global on purpose,
 * so without this a suite that exercises a missing mark retires that service
 * for every test that follows it in the same file. */
export function forgetFailedServiceMarks(): void {
  markFailedServiceNames.clear();
}

const MARK_IMG_CLASS = "service-mark-img";
const LIGHT_SURFACE_IMG_CLASS = "service-mark-img on-light-surface";
const DARK_SURFACE_IMG_CLASS = "service-mark-img on-dark-surface";

function markUrl(fileName: string): string {
  return `/_static/service_icons/${encodeURIComponent(fileName)}.svg`;
}

/** `onMissing` is passed only for the mark a surface cannot do without: a
 * missing dark variant must not retire a base logo that loads perfectly well. */
function markImg(url: string, imgClass: string, onMissing: (() => void) | null): m.Children {
  return m("img", {
    src: url,
    alt: "",
    class: imgClass,
    ...(onMissing === null ? {} : { onerror: onMissing }),
  });
}

/** The service's logo, or `fallback` when it has none.
 *
 * Drawn as an <img> rather than a CSS mask: a mask keeps only the artwork's
 * alpha and throws its color away, and these logos are full-color. An <img> is
 * also an isolated document, which the artwork requires -- Notion's mark pairs
 * a white path with an unfilled one, so inlining it under a `fill:
 * currentColor` ancestor would repaint it. Do not inline these.
 *
 * The <img> is its own load probe, which is why the mask's separate hidden
 * probe is gone: a 404 records the service, and the redraw that follows the
 * error event draws `fallback` instead.
 *
 * `sizeClass` must be passed as a complete literal -- Tailwind matches source
 * text, so a class assembled from parts would render unstyled. */
export function serviceMark(
  serviceName: string,
  sizeClass: string,
  tone: MarkTone,
  fallback: m.Children,
): m.Children {
  if (!serviceName || markFailedServiceNames.has(serviceName)) return fallback;
  const recordMissing = (): void => {
    markFailedServiceNames.add(serviceName);
  };
  const wrapperClass =
    (tone === "muted" ? "service-mark service-mark-muted " : "service-mark ") + sizeClass;
  if (!DARK_SURFACE_VARIANT_SERVICE_NAMES.has(serviceName)) {
    return m(
      "span",
      { class: wrapperClass },
      markImg(markUrl(serviceName), MARK_IMG_CLASS, recordMissing),
    );
  }
  return m("span", { class: wrapperClass }, [
    markImg(markUrl(serviceName), LIGHT_SURFACE_IMG_CLASS, recordMissing),
    markImg(markUrl(`${serviceName}-on-dark`), DARK_SURFACE_IMG_CLASS, null),
  ]);
}
