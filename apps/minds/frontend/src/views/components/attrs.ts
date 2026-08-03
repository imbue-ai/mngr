import type m from "mithril";

// Split a component's incoming attrs into the keys the component consumes
// and the passthrough HTML attributes (id, aria-*, data-*, event handlers,
// ...) -- the SPA equivalent of JinjaX's ``attrs.render`` passthrough. The
// component names its own keys; everything else lands on the root element.
//
// `class`/`className` never pass through: components spread the passthrough
// after their computed class, so a caller's class would silently replace the
// entire recipe. Additive classes go through each component's `extra` attr.
export function splitAttrs<T extends m.Attributes>(
  attrs: T,
  ownKeys: readonly (keyof T)[],
): m.Attributes {
  const passthrough: m.Attributes = {};
  for (const key of Object.keys(attrs)) {
    if (key === "class" || key === "className") continue;
    if (!(ownKeys as readonly string[]).includes(key)) {
      passthrough[key] = (attrs as m.Attributes)[key];
    }
  }
  return passthrough;
}

// Join class fragments, dropping empties -- mirrors the Jinja
// ``reject("equalto", "") | join(" ")`` idiom used by Card.jinja.
export function joinClasses(
  ...fragments: (string | false | undefined)[]
): string {
  return fragments
    .filter(
      (fragment) =>
        fragment !== undefined && fragment !== false && fragment !== "",
    )
    .join(" ");
}
