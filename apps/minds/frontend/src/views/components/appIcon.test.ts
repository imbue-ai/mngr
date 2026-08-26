// @vitest-environment jsdom
//
// DOMPurify needs a real parser, and refusing to sanitize without one is part
// of the contract under test.
import { describe, expect, it } from "vitest";

import { appMonogramMarkup, sanitizeIconMarkup, shareTargetIconMarkup } from "./appIcon";

const PLAIN_ICON = '<svg viewBox="0 0 24 24"><path d="M2 2h20v20H2z"/></svg>';

describe("sanitizeIconMarkup", () => {
  it("keeps an ordinary icon's art, sized to the caller's grid", () => {
    const result = sanitizeIconMarkup(PLAIN_ICON, 16);
    expect(result).toContain('d="M2 2h20v20H2z"');
    expect(result).toContain('width="16"');
    expect(result).toContain('height="16"');
    expect(result).toContain('viewBox="0 0 24 24"');
  });

  it("derives a viewBox from a sized icon, and refuses sizeless art", () => {
    expect(sanitizeIconMarkup('<svg width="32" height="32"><path d="M1 1"/></svg>', 16)).toContain(
      'viewBox="0 0 32 32"',
    );
    expect(sanitizeIconMarkup('<svg><path d="M1 1"/></svg>', 16)).toBeNull();
  });

  it("lets a monochrome icon inherit currentColor and leaves a painted one alone", () => {
    expect(sanitizeIconMarkup(PLAIN_ICON, 16)).toContain('fill="currentColor"');
    const painted = sanitizeIconMarkup('<svg viewBox="0 0 24 24" fill="#f00"><path d="M1 1"/></svg>', 16);
    expect(painted).toContain('fill="#f00"');
  });

  it("strips everything executable or resource-reaching", () => {
    const hostile = [
      '<svg viewBox="0 0 24 24"><script>alert(1)</script><path d="M1 1"/></svg>',
      '<svg viewBox="0 0 24 24" onload="alert(1)"><path d="M1 1"/></svg>',
      '<svg viewBox="0 0 24 24"><a href="javascript:alert(1)"><path d="M1 1"/></a></svg>',
      '<svg viewBox="0 0 24 24"><image href="https://evil.example/x.png"/><path d="M1 1"/></svg>',
      '<svg viewBox="0 0 24 24"><style>path{fill:url(https://evil.example)}</style><path d="M1 1"/></svg>',
      '<svg viewBox="0 0 24 24"><foreignObject><div>x</div></foreignObject><path d="M1 1"/></svg>',
      '<svg viewBox="0 0 24 24"><animate attributeName="href" to="javascript:alert(1)"/><path d="M1 1"/></svg>',
      '<svg viewBox="0 0 24 24"><path d="M1 1" fill="url(https://evil.example)"/></svg>',
    ];
    for (const markup of hostile) {
      const result = sanitizeIconMarkup(markup, 16);
      for (const trace of ["script", "onload", "javascript", "evil.example", "foreignObject", "animate", "style"]) {
        expect(result ?? "", markup).not.toContain(trace);
      }
    }
  });

  it("refuses markup that is not exactly one svg element", () => {
    expect(sanitizeIconMarkup(`<div>${PLAIN_ICON}</div>`, 16)).toBeNull();
    expect(sanitizeIconMarkup(`${PLAIN_ICON}${PLAIN_ICON}`, 16)).toBeNull();
    expect(sanitizeIconMarkup("plain text", 16)).toBeNull();
    expect(sanitizeIconMarkup("", 16)).toBeNull();
    expect(sanitizeIconMarkup("<svg " + "a".repeat(17000) + "/>", 16)).toBeNull();
  });

  it("namespaces ids so two apps' gradients cannot collide", () => {
    const withGradient =
      '<svg viewBox="0 0 24 24"><defs><linearGradient id="g"/></defs><path d="M1 1" fill="url(#g)"/></svg>';
    const result = sanitizeIconMarkup(withGradient, 16);
    expect(result).not.toContain('id="g"');
    expect(result).toMatch(/id="app-icon-[a-z0-9]+-g"/);
    expect(result).toMatch(/fill="url\(#app-icon-[a-z0-9]+-g\)"/);
  });
});

describe("shareTargetIconMarkup", () => {
  it("uses the registered icon when usable and the monogram otherwise", () => {
    expect(shareTargetIconMarkup(PLAIN_ICON, "notes", 16)).toContain('d="M2 2h20v20H2z"');
    expect(shareTargetIconMarkup("", "notes", 16)).toBe(appMonogramMarkup("notes", 16));
    expect(shareTargetIconMarkup("<svg onload='x'>", "notes", 16)).toBe(appMonogramMarkup("notes", 16));
  });

  it("monograms with the escaped initial", () => {
    expect(appMonogramMarkup("notes", 16)).toContain(">N</text>");
    expect(appMonogramMarkup("<x>", 16)).toContain(">&lt;</text>");
  });
});
