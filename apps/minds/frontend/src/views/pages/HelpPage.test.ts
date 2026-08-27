import { describe, expect, it } from "vitest";
import { HelpModel, setPendingHelpLaunch } from "../../models/help";
import {
  allText,
  classTokensOf,
  collectVnodes,
  memoryStorage,
} from "../../testing";
import type { AnyVnode } from "../../testing";
import { formPhase, sentPhase, titleRow } from "./HelpPage";

/** Render the report form for a help surface opened over `workspaceAgentId`. */
function renderForm(workspaceAgentId: string): AnyVnode[] {
  setPendingHelpLaunch({ workspaceAgentId });
  return collectVnodes(formPhase(new HelpModel({ storage: memoryStorage() })));
}

function checkboxIds(nodes: AnyVnode[]): unknown[] {
  return nodes
    .filter((node) => node.tag === "input" && node.attrs?.type === "checkbox")
    .map((node) => node.attrs?.id);
}

function findById(nodes: AnyVnode[], id: string): AnyVnode | undefined {
  return nodes.find((node) => node.attrs?.id === id);
}

describe("HelpPage title row", () => {
  it("mirrors the feed's header: 56px row, bug icon left of the label, hairline below", () => {
    // The help form and the notification feed are one anchored window shown
    // two ways, so a switch between them must keep one header line.
    const nodes = collectVnodes(titleRow());
    const row = nodes[0];

    const rowClasses = classTokensOf(row);
    expect(rowClasses).toContain("h-[56px]");
    expect(rowClasses).toContain("border-b");

    const heading = nodes.find((node) => node.tag === "h1");
    expect(heading).toBeDefined();
    expect(classTokensOf(heading as AnyVnode)).toContain("type-label");
    const icon = collectVnodes((heading as AnyVnode).children).find(
      (node) => node.attrs?.name !== undefined,
    );
    expect(icon?.attrs?.name).toBe("bug");
    expect(allText(titleRow())).toContain("Ran into a bug?");
  });
});

describe("HelpPage report form", () => {
  it("offers the workspace diagnostics checkboxes above remote access, both checked", () => {
    const nodes = renderForm("agent-1");

    expect(checkboxIds(nodes)).toEqual([
      "help-include-logs",
      "help-include-transcript",
      "help-remote-access",
    ]);
    expect(findById(nodes, "help-include-logs")?.attrs?.checked).toBe(true);
    expect(findById(nodes, "help-include-transcript")?.attrs?.checked).toBe(
      true,
    );
  });

  it("hides both diagnostics checkboxes when the report is not scoped to a machine", () => {
    const nodes = renderForm("");

    // Nothing can be collected from inside a machine, so neither box is offered.
    expect(checkboxIds(nodes)).toEqual(["help-remote-access"]);
    const rendered = JSON.stringify(nodes);
    expect(rendered).not.toContain("Include workspace logs");
    expect(rendered).not.toContain("Include recent chats");
  });

  it("renders an unchecked box for a preference the user has already turned off", () => {
    const storage = memoryStorage();
    storage.setItem("minds.help.help-include-transcript", "false");
    setPendingHelpLaunch({ workspaceAgentId: "agent-1" });

    const nodes = collectVnodes(formPhase(new HelpModel({ storage })));

    expect(findById(nodes, "help-include-logs")?.attrs?.checked).toBe(true);
    expect(findById(nodes, "help-include-transcript")?.attrs?.checked).toBe(
      false,
    );
  });

  it("disables the submit button while a submission is in flight", () => {
    setPendingHelpLaunch({ workspaceAgentId: "agent-1" });
    const model = new HelpModel({ storage: memoryStorage() });

    expect(
      findById(collectVnodes(formPhase(model)), "help-submit")?.attrs?.disabled,
    ).toBe(false);
    model.isSubmitBusy = true;
    expect(
      findById(collectVnodes(formPhase(model)), "help-submit")?.attrs?.disabled,
    ).toBe(true);
  });

  it("writes each box straight through to the model and its sticky key", () => {
    const storage = memoryStorage();
    setPendingHelpLaunch({ workspaceAgentId: "agent-1" });
    const model = new HelpModel({ storage });
    const nodes = collectVnodes(formPhase(model));

    const uncheck = (id: string): void => {
      const onchange = findById(nodes, id)?.attrs?.onchange as (
        event: Event,
      ) => void;
      onchange({ target: { checked: false } } as unknown as Event);
    };
    uncheck("help-include-logs");
    uncheck("help-include-transcript");

    expect(model.isLogsIncluded).toBe(false);
    expect(model.isTranscriptIncluded).toBe(false);
    expect(storage.values.get("minds.help.help-include-logs")).toBe("false");
    expect(storage.values.get("minds.help.help-include-transcript")).toBe(
      "false",
    );
  });

  it("warns that a withheld diagnostic may cost them the fix, and only inside a machine", () => {
    setPendingHelpLaunch({ workspaceAgentId: "agent-1" });
    const model = new HelpModel({ storage: memoryStorage() });

    // Both included: nothing is being withheld, so there is nothing to warn about.
    expect(
      findById(collectVnodes(formPhase(model)), "help-withheld-warning"),
    ).toBeUndefined();

    // The sentence names what is missing, so it says which box to tick.
    const warningText = (): string =>
      allText(
        findById(collectVnodes(formPhase(model)), "help-withheld-warning"),
      );

    model.setLogsIncluded(false);
    expect(warningText()).toContain(
      "We may not be able to solve this issue without workspace logs.",
    );

    model.setTranscriptIncluded(false);
    expect(warningText()).toContain(
      "We may not be able to solve this issue without workspace logs and recent chats.",
    );

    model.setLogsIncluded(true);
    expect(warningText()).toContain(
      "We may not be able to solve this issue without recent chats.",
    );

    // Outside a machine the boxes are never offered, so the warning would be a
    // non-sequitur rather than a caution.
    setPendingHelpLaunch({ workspaceAgentId: "" });
    const unscoped = new HelpModel({ storage: memoryStorage() });
    expect(
      findById(collectVnodes(formPhase(unscoped)), "help-withheld-warning"),
    ).toBeUndefined();
  });

  it("reddens the label of a box the user unticked", () => {
    setPendingHelpLaunch({ workspaceAgentId: "agent-1" });
    const model = new HelpModel({ storage: memoryStorage() });

    // classesOf, not attrs.class: mithril normalizes `class` into `className`,
    // so reading attrs.class directly comes back null.
    const labelClassFor = (id: string): string[] => {
      const label = findById(collectVnodes(formPhase(model)), `${id}-label`);
      return label === undefined ? [] : classTokensOf(label);
    };

    expect(labelClassFor("help-include-logs")).toContain("text-primary");

    model.setLogsIncluded(false);
    expect(labelClassFor("help-include-logs")).toContain("text-important");
    // The box still ticked keeps its ordinary colour.
    expect(labelClassFor("help-include-transcript")).toContain("text-primary");
  });

  it("gives each opt-out checkbox the reason it is being asked for", () => {
    setPendingHelpLaunch({ workspaceAgentId: "agent-1" });
    const rendered = allText(
      collectVnodes(formPhase(new HelpModel({ storage: memoryStorage() }))),
    );

    // Both boxes exist for the same reason -- diagnosing the issue -- and no
    // consent reassurance rides along: ticking the box IS the consent.
    expect(rendered).toContain("We'll need these to diagnose the issue.");
    expect(rendered).not.toContain("We will never access them");
    // The trailing prose block these reasons replaced is gone.
    expect(rendered).not.toContain("App diagnostics (app version");
  });
});

describe("HelpPage sent screen", () => {
  /** Render the sent screen for a report submitted with the given boxes ticked. */
  function renderSent(
    workspaceAgentId: string,
    { isLogsIncluded = true, isTranscriptIncluded = true } = {},
  ): AnyVnode[] {
    setPendingHelpLaunch({ workspaceAgentId });
    const model = new HelpModel({ storage: memoryStorage() });
    model.isLogsIncluded = isLogsIncluded;
    model.isTranscriptIncluded = isTranscriptIncluded;
    model.sentEventId = "abc123";
    return collectVnodes(sentPhase(model));
  }

  it("shows the report ID as a click-to-copy chip once sent", () => {
    const nodes = renderSent("agent-1");

    expect(JSON.stringify(nodes)).toContain("abc123");
    expect(findById(nodes, "help-report-id")).toBeDefined();
  });
});
