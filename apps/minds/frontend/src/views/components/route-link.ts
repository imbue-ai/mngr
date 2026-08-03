import m from "mithril";

// Anchor attrs for INTERNAL navigation: keep the real href (hover preview,
// copy-link, middle-click) but intercept the click so the router handles it
// -- with m.route.prefix = "" a bare href would otherwise trigger a full
// document reload (or, for "#!/...", nothing at all). Server-owned routes
// like /auth/login must NOT use this; they are real navigations.
export function routeLinkAttrs(target: string): m.Attributes {
  return {
    href: target,
    onclick: (event: MouseEvent) => {
      event.preventDefault();
      m.route.set(target);
    },
  };
}
