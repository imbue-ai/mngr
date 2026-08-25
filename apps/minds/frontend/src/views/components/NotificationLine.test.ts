import { describe, expect, it } from "vitest";
import type { UiNotificationEntry } from "../../channel/messages";
import type { AnyVnode } from "../../testing";
import {
  allText,
  attrsOf,
  classTokensOf,
  collectVnodes,
  notificationEntry,
} from "../../testing";
import { notificationLine, timeAgo } from "./NotificationLine";

const NOW = Date.parse("2026-08-18T12:00:00Z");

function at(secondsAgo: number): string {
  return new Date(NOW - secondsAgo * 1000).toISOString();
}

describe("timeAgo", () => {
  it("labels coarse buckets from seconds through days", () => {
    const cases: [string, string][] = [
      [at(0), "just now"],
      [at(9), "just now"],
      [at(10), "10s ago"],
      [at(59), "59s ago"],
      [at(60), "1m ago"],
      [at(150), "3m ago"],
      [at(59 * 60), "59m ago"],
      [at(60 * 60), "1h ago"],
      [at(5 * 60 * 60), "5h ago"],
      [at(24 * 60 * 60), "1d ago"],
      [at(3 * 24 * 60 * 60), "3d ago"],
    ];
    for (const [iso, expected] of cases) {
      expect(timeAgo(iso, NOW), iso).toBe(expected);
    }
  });

  it("clamps clock skew to 'just now' and blanks unparseable timestamps", () => {
    expect(timeAgo(at(-30), NOW)).toBe("just now");
    expect(timeAgo("not-a-date", NOW)).toBe("");
    expect(timeAgo("", NOW)).toBe("");
  });
});

/** The line renders no timestamp itself, so only the display fields vary. */
function entry(
  overrides: Partial<UiNotificationEntry> = {},
): UiNotificationEntry {
  return notificationEntry("n1", { service_name: "slack", ...overrides });
}

describe("notificationLine", () => {
  it("leads with the accent dot, then the bold workspace and ask joined by an em dash", () => {
    const root = notificationLine({ entry: entry() }) as unknown as AnyVnode;
    const dot = collectVnodes(root).find((vnode) =>
      String(attrsOf(vnode).style ?? "").includes("background-color: #aabbcc"),
    );
    expect(dot).toBeDefined();
    const text = allText(root);
    expect(text).toContain("alpha");
    expect(text).toContain(" asks — ");
    expect(text).toContain("Slack access");
    const bolded = collectVnodes(root)
      .filter((vnode) => classTokensOf(vnode).includes("font-semibold"))
      .map((vnode) => allText(vnode));
    expect(bolded).toEqual(["alpha", "Slack access"]);
  });

  it("clamps the body to two lines and drops the line when the body is empty", () => {
    const withBody = notificationLine({
      entry: entry(),
    }) as unknown as AnyVnode;
    const body = collectVnodes(withBody).find((vnode) =>
      classTokensOf(vnode).includes("line-clamp-2"),
    );
    expect(body).toBeDefined();
    expect(allText(body)).toBe("wants to read messages");

    const without = notificationLine({
      entry: entry({ body: "" }),
    }) as unknown as AnyVnode;
    expect(
      collectVnodes(without).some((vnode) =>
        classTokensOf(vnode).includes("line-clamp-2"),
      ),
    ).toBe(false);
  });

  it("shows the service brand mark only when a service is named", () => {
    const marked = notificationLine({ entry: entry() }) as unknown as AnyVnode;
    expect(
      collectVnodes(marked).some((vnode) =>
        classTokensOf(vnode).includes("service-mark"),
      ),
    ).toBe(true);

    const unmarked = notificationLine({
      entry: entry({ service_name: "" }),
    }) as unknown as AnyVnode;
    expect(
      collectVnodes(unmarked).some((vnode) =>
        classTokensOf(vnode).includes("service-mark"),
      ),
    ).toBe(false);
  });

  it("renders the caller's meta beside the sentence and footer below the body", () => {
    const root = notificationLine({
      entry: entry(),
      meta: "META-TEXT",
      footer: "FOOTER-TEXT",
    }) as unknown as AnyVnode;
    const text = allText(root);
    expect(text).toContain("META-TEXT");
    expect(text).toContain("FOOTER-TEXT");
  });
});
