// Create from Template (GET /create/template?git_url=...): the landing
// stepper for a template deeplink, a numbered vertical stepper that shows
// ONE step at a time (completed steps collapse to their number + a one-line
// summary, click to reopen). Port of TemplateCreate.jinja.
//
//   Step 1: create a NEW machine from the repo, or add it to an EXISTING one.
//   Create branch: (2) local vs remote presets, (3) confirm -- account, a
//     required "I trust this Template" acknowledgment, optional advanced
//     provider/region settings, and Create.
//   Add branch: (2) copy the adopt-command message (copying
//     advances), (3) pick the machine to open (full page) -- or, in the modal,
//     just paste it into the machine you are already in.
//
// One stepper, two shells: the Shell floats it as a modal over a live machine
// when ?workspace= is present (the add branch then targets THAT machine, and
// "Create a new machine" hands off to the full page -- creating is a bigger job
// than a popup should host); otherwise it is the full page.

import m from "mithril";
import { getAppContext } from "../../app-context";
import { MIND_LIVENESS_LABELS, fetchCreateFormDefaults, recoveryRoute } from "../../models/create";
import type { UiWorkspaceEntry } from "../../channel/messages";
import { Button, ButtonSubmit } from "../components/Button";
import { Card } from "../components/Card";
import { FormLabel, Select } from "../components/FormControls";
import { PageNarrowContainer } from "../components/Layout";
import { Icon16 } from "../components/Icon";
import { StatusBadge } from "../components/StatusBadge";
import { rowClickActionFor } from "./landing-controls";
import { PresetCards } from "./create/PresetCards";
import type { PresetName } from "./create/form-model";
import { CreateFormModel, normalizeCreateApiError } from "./create/form-model";

// The chat command the add flow tells the user to paste.
//
// The LEADING SPACE is deliberate. Pasting a string that starts with "/" into
// a chat input can be taken as a slash command by the client rather than as
// message text; a space in front makes it unambiguous while still reading and
// running the same once sent.
//
// Requires the target machine to have the `use-template` skill, i.e. to have
// been created from (or updated to) a post-rename workspace template. A machine
// on an older template will not recognise it.
const ADOPT_COMMAND = " /use-template";


const ID_SEGMENT = /^(?:agent|host)-[a-f0-9]+$/i;

// A soft ease-out shared by every reveal so the motion feels consistent
// (ported from TemplateCreate.jinja's animations).
const EASE_OUT = "cubic-bezier(0.16, 1, 0.3, 1)";

/** Slide + fade an element into place, easing in gently -- played once when a
 * step's body (or the advanced panel) is first shown. */
function animateReveal(dom: Element, distance: number, duration: number): void {
  if (typeof dom.animate !== "function") return;
  dom.animate(
    [
      { opacity: 0, transform: `translateY(${distance}px)` },
      { opacity: 0.6, offset: 0.4 },
      { opacity: 1, transform: "translateY(0)" },
    ],
    { duration, easing: EASE_OUT, fill: "both" },
  );
}

/** A gentle overshoot when a step's circle fills in on completion. */
function animatePop(dom: Element): void {
  if (typeof dom.animate !== "function") return;
  dom.animate(
    [{ transform: "scale(0.8)" }, { transform: "scale(1.06)", offset: 0.6 }, { transform: "scale(1)" }],
    { duration: 420, easing: "cubic-bezier(0.34, 1.56, 0.64, 1)" },
  );
}

/** Grow a dashed divider rule out from the label side as the advanced panel
 * opens (transformOrigin set inline per side). */
function animateRule(dom: Element): void {
  if (typeof dom.animate !== "function") return;
  dom.animate(
    [
      { transform: "scaleX(0)", opacity: 0 },
      { transform: "scaleX(1)", opacity: 1 },
    ],
    { duration: 440, easing: EASE_OUT },
  );
}

interface StepSpec {
  number: number;
  title: string;
  target: number;
  active: boolean;
  done: boolean;
  summary: string;
  body: () => m.Children;
}

export const CreateTemplatePage: m.ClosureComponent = () => {
  const model = new CreateFormModel();
  // Route-derived (re-synced each render so the modal -> full page create hand-
  // off, which keeps this component instance, is picked up).
  let routeKey = "";
  let gitUrl = "";
  let branchName = "";
  let machineAnyId: string | null = null; // set -> modal over this machine
  // Flow state (a two-variable state machine: which branch, which step is open).
  let branch: "create" | "add" | null = null;
  let activeStep = 1;
  let isAdvanced = false;
  let isMessageCopied = false;
  let isTrusted = false;
  let isAccountErrorShown = false;
  let submitError = "";
  const copyTimers: ReturnType<typeof setTimeout>[] = [];
  // Which step circles have already played their completion pop, so a redraw
  // does not replay it (cleared if the step reopens).
  const poppedSteps = new Set<number>();

  function resetFlow(start: string): void {
    branch = start === "create" || start === "add" ? start : null;
    activeStep = branch === null ? 1 : 2;
    isAdvanced = false;
    isMessageCopied = false;
    isTrusted = false;
    isAccountErrorShown = false;
    submitError = "";
    model.selectedPreset = null;
  }

  /** Re-read the route so a same-route param change (the modal's create hand-off
   * to the full page) re-initializes the flow; returns false when there is no
   * repo (caller redirects to the plain create form). */
  function syncRoute(): boolean {
    const nextGitUrl = m.route.param("git_url") ?? "";
    const workspaceParam = m.route.param("workspace") ?? "";
    const start = m.route.param("start") ?? "";
    const key = `${nextGitUrl}|${workspaceParam}|${start}`;
    if (key === routeKey) return nextGitUrl !== "";
    routeKey = key;
    gitUrl = nextGitUrl;
    branchName = m.route.param("branch") ?? "";
    machineAnyId = ID_SEGMENT.test(workspaceParam) ? workspaceParam : null;
    if (gitUrl === "") return false;
    resetFlow(start);
    return true;
  }

  function isModal(): boolean {
    return machineAnyId !== null;
  }

  function machineName(): string {
    return machineAnyId !== null
      ? (getAppContext().stores.workspaces.accentEntry(machineAnyId)?.name ?? "this machine")
      : "";
  }

  function goBack(target: number): void {
    if (activeStep > target) {
      activeStep = target;
    }
  }

  // The full page vertically centers its column (PageNarrowContainer). Opening
  // the advanced panel can make the content taller than the viewport, which
  // would re-center it and yank everything upward. Freeze the column at its
  // current top BEFORE the panel grows -- switch the centering container off
  // center-alignment and compensate with a margin -- so growth pushes downward
  // instead of jumping up. Released when the panel is removed. (Ported from
  // TemplateCreate.jinja; the modal card scrolls itself, so it is exempt.)
  let isColumnPinned = false;
  function pageColumn(): HTMLElement | null {
    return document.getElementById("template-content-top")?.parentElement ?? null;
  }
  function pinPageColumn(): void {
    if (isModal() || isColumnPinned) return;
    const column = pageColumn();
    const container = column?.parentElement;
    if (!column || !container) return;
    const before = column.getBoundingClientRect().top;
    container.style.alignItems = "flex-start";
    const after = column.getBoundingClientRect().top;
    column.style.marginTop = `${before - after}px`;
    isColumnPinned = true;
  }
  function unpinPageColumn(): void {
    if (!isColumnPinned) return;
    const column = pageColumn();
    const container = column?.parentElement;
    if (column) column.style.marginTop = "";
    if (container) container.style.alignItems = "";
    isColumnPinned = false;
  }

  function chooseBranch(name: "create" | "add"): void {
    if (branch !== name) {
      branch = name;
      isMessageCopied = false;
      model.selectedPreset = null;
      isAdvanced = false;
    }
    activeStep = 2;
  }

  // Creating a new machine is a bigger job than a popup should host, so from the
  // modal it hands off to the full page (already past the chooser, start=create,
  // no ?workspace); on the page it just advances.
  function chooseCreate(): void {
    if (isModal()) {
      const params: Record<string, string> = { git_url: gitUrl, start: "create" };
      if (branchName) params.branch = branchName;
      m.route.set("/create/template", params);
      return;
    }
    chooseBranch("create");
  }

  function doCopy(): void {
    if (isMessageCopied) return;
    navigator.clipboard?.writeText(`${ADOPT_COMMAND} ${gitUrl}`).catch(() => undefined);
    isMessageCopied = true;
    // Copying is what advances to the last step, so the message is always in
    // hand first; hold long enough that the green confirmation registers.
    copyTimers.push(
      setTimeout(() => {
        activeStep = 3;
        m.redraw();
      }, 1000),
    );
    // Reset the green after it has collapsed, so reopening step 2 is fresh.
    copyTimers.push(
      setTimeout(() => {
        isMessageCopied = false;
        m.redraw();
      }, 2200),
    );
  }

  function submitCreate(event: Event): void {
    event.preventDefault();
    if (!isTrusted) return;
    model.gitUrl = gitUrl;
    model.branch = branchName;
    if (model.imbueCloudNeedsAccount()) {
      if ((model.defaults?.accounts.length ?? 0) > 0) {
        isAccountErrorShown = true;
      } else {
        m.route.set("/accounts");
      }
      return;
    }
    model.isSubmitting = true;
    submitError = "";
    fetch("/api/v1/workspaces", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(model.submitBody()),
    })
      .then(async (response) => ({
        status: response.status,
        data: (await response.json().catch(() => ({}))) as Record<string, unknown>,
      }))
      .then((result) => {
        if (result.status === 202 && typeof result.data.operation_id === "string") {
          m.route.set(`/creating/${result.data.operation_id}`);
          return;
        }
        model.isSubmitting = false;
        submitError = normalizeCreateApiError(result.data).message;
        m.redraw();
      })
      .catch(() => {
        model.isSubmitting = false;
        submitError = "Could not reach the server. Please try again.";
        m.redraw();
      });
  }

  function machineRow(entry: UiWorkspaceEntry): m.Children {
    const { shell } = getAppContext();
    const liveness = entry.liveness ?? "";
    // Badge the settled-down and transitional states; UNKNOWN stays unbadged
    // here (this page has no liveness tracker, so an unknown reading is
    // common and not actionable from a template picker).
    const isBadged = liveness === "STOPPED" || liveness === "STOPPING" || liveness === "STARTING";
    const livenessLabel = (entry.supports_shutdown ?? false) && isBadged ? MIND_LIVENESS_LABELS[liveness] : null;
    return m(
      Card,
      {
        layout: "row",
        interactive: true,
        extra: "accent-spine relative overflow-hidden cursor-pointer",
        style: `--workspace-accent: ${entry.accent};`,
        onclick: () => {
          // This page has no health tracker, so the row is treated as healthy
          // (the pre-existing behavior: it never routes to plain recovery).
          const action = rowClickActionFor(entry, entry.liveness ?? "", true);
          if (action === "recover-start") {
            const returnTo = `/goto/${entry.id}/`;
            m.route.set(recoveryRoute(entry.id, returnTo, "start"));
          } else {
            shell.enterWorkspace(entry.id);
          }
        },
      },
      [
        m("span", { class: "flex-1 min-w-0 truncate font-semibold text-primary pl-1" }, entry.name),
        livenessLabel ? m(StatusBadge, livenessLabel) : null,
        m("span", { class: "text-tertiary shrink-0" }, m(Icon16, { name: "chevron-right" })),
      ],
    );
  }

  // ---- Step bodies ----

  function step1Body(): m.Children {
    return m("div", { class: "flex flex-col gap-3" }, [
      m(Button, { variant: "secondary", block: true, onclick: () => chooseCreate() }, "Create a new machine"),
      m(
        Button,
        { variant: "secondary", block: true, onclick: () => chooseBranch("add") },
        isModal() ? `Add to ${machineName()}` : "Add to an existing machine",
      ),
    ]);
  }

  function createPresetBody(): m.Children {
    return m(
      "div",
      { role: "radiogroup", "aria-label": "Where to run your machine" },
      m(PresetCards, {
        selectedPreset: model.selectedPreset,
        onSelect: (name: PresetName) => {
          model.applyPreset(name);
          isAdvanced = false;
          activeStep = 3;
        },
      }),
    );
  }

  function advancedPanel(): m.Children {
    const defaults = model.defaults;
    if (defaults === null) return null;
    const regionOptions = model.regionOptions();
    return m("div", { class: "mt-4 flex flex-col gap-6" }, [
      m("div", { class: "flex items-center justify-between gap-4" }, [
        m(FormLabel, { target: "insp-launch-mode", inline: true }, "Compute provider"),
        m(
          Select,
          {
            id: "insp-launch-mode",
            name: "launch_mode",
            width: "w-48",
            value: model.launchValue,
            onchange: (event: Event) => {
              model.launchValue = (event.target as HTMLSelectElement).value;
            },
          },
          defaults.launch_modes.map((mode) =>
            m("option", { value: mode, selected: model.launchValue === mode }, mode.toLowerCase()),
          ),
        ),
      ]),
      m("div", { class: "flex items-center justify-between gap-4" }, [
        m(FormLabel, { target: "insp-backup-provider", inline: true }, "Backup provider"),
        m(
          Select,
          {
            id: "insp-backup-provider",
            name: "backup_provider",
            width: "w-48",
            value: model.backupProvider,
            onchange: (event: Event) => {
              model.backupProvider = (event.target as HTMLSelectElement).value;
            },
          },
          defaults.backup_providers.map((provider) =>
            m(
              "option",
              { value: provider, selected: model.backupProvider === provider },
              provider === "API_KEY" ? "manual" : provider.toLowerCase(),
            ),
          ),
        ),
      ]),
      regionOptions.length > 0
        ? m("div", { class: "flex items-center justify-between gap-4" }, [
            m(FormLabel, { target: "insp-region", inline: true }, "Region"),
            m(
              Select,
              {
                id: "insp-region",
                name: "region",
                width: "w-48",
                value: model.selectedRegion(),
                onchange: (event: Event) => model.setRegion((event.target as HTMLSelectElement).value),
              },
              regionOptions.map((region) =>
                m("option", { value: region, selected: region === model.selectedRegion() }, region),
              ),
            ),
          ])
        : null,
    ]);
  }

  function createConfirmBody(): m.Children {
    const defaults = model.defaults;
    if (defaults === null) return m("p", { class: "type-helper text-tertiary" }, "Loading…");
    return m("form", { onsubmit: submitCreate }, [
      submitError ? m("p", { role: "alert", class: "mb-4 type-helper text-important" }, submitError) : null,
      m("div", { class: "mb-4" }, [
        m("p", { class: "type-label" }, "Creating from"),
        m("p", { class: "type-body text-primary font-mono mt-1 break-all" }, gitUrl),
        branchName ? m("p", { class: "type-helper text-tertiary mt-1" }, `Version: ${branchName}`) : null,
      ]),
      m("div", { class: "mb-4" }, [
        m("p", { class: "type-label" }, "Account"),
        m(
          "div",
          { class: "mt-1" },
          m(
            Select,
            {
              id: "account_id",
              name: "account_id",
              width: "w-72",
              value: model.accountId,
              onchange: (event: Event) => {
                model.accountId = (event.target as HTMLSelectElement).value;
                if (!model.imbueCloudNeedsAccount()) isAccountErrorShown = false;
              },
            },
            [
              ...defaults.accounts.map((account) =>
                m("option", { value: account.user_id, selected: model.accountId === account.user_id }, account.email),
              ),
              m("option", { value: "", selected: model.accountId === "" }, "No account (private machine)"),
            ],
          ),
        ),
      ]),
      isAccountErrorShown
        ? m(
            "p",
            { class: "mb-4 type-helper text-important" },
            "Pick an account to use Imbue Cloud, or choose the local preset.",
          )
        : null,
      m(
        "label",
        { class: "mt-6 flex items-center gap-3 cursor-pointer" },
        [
          m("input", {
            type: "checkbox",
            class: "shrink-0",
            checked: isTrusted,
            onchange: (event: Event) => {
              isTrusted = (event.target as HTMLInputElement).checked;
            },
          }),
          m("span", [
            m(
              "span",
              { class: "type-body font-semibold " + (isTrusted ? "text-primary" : "text-important") },
              "I trust this Template",
            ),
            m(
              "span",
              { class: "block type-helper text-primary" },
              "Templates are community content. This one has not been approved or verified by Imbue.",
            ),
          ]),
        ],
      ),
      m("div", { class: "mt-8" }, [
        m(
          "button",
          {
            type: "button",
            // The label stays centered whether or not the flanking rules are
            // shown, so opening/closing never shifts it (no jump to compensate).
            class: "group w-full flex items-center justify-center gap-3 cursor-pointer bg-transparent",
            onclick: () => {
              if (isAdvanced) {
                isAdvanced = false;
              } else {
                // Freeze the centered page column BEFORE the panel grows so the
                // extra height pushes downward instead of yanking it upward.
                pinPageColumn();
                isAdvanced = true;
              }
            },
          },
          [
            isAdvanced
              ? m("span", {
                  class: "flex-1 border-t border-dashed border-default",
                  style: "transform-origin: right center;",
                  oncreate: (vnode: m.VnodeDOM) => animateRule(vnode.dom),
                })
              : null,
            m("span", { class: "flex items-center gap-1 type-helper text-tertiary group-hover:text-primary" }, [
              "Advanced settings",
              m(
                "span",
                { class: "transition-transform duration-200", style: isAdvanced ? "transform: rotate(180deg);" : "" },
                m(Icon16, { name: "chevron-down" }),
              ),
            ]),
            isAdvanced
              ? m("span", {
                  class: "flex-1 border-t border-dashed border-default",
                  style: "transform-origin: left center;",
                  oncreate: (vnode: m.VnodeDOM) => animateRule(vnode.dom),
                })
              : null,
          ],
        ),
        isAdvanced
          ? m(
              "div",
              {
                oncreate: (vnode: m.VnodeDOM) => animateReveal(vnode.dom, 12, 440),
                // Release the pin however the panel closes (toggle, step-back, or unmount).
                onremove: () => unpinPageColumn(),
              },
              advancedPanel(),
            )
          : null,
      ]),
      m(
        "div",
        { class: "mt-6" },
        m(
          ButtonSubmit,
          { variant: "primary", block: true, disabled: !isTrusted || model.isSubmitting },
          model.isSubmitting ? "Creating..." : "Create from Template",
        ),
      ),
    ]);
  }

  function addCopyBody(): m.Children {
    return m("div", [
      m(
        "p",
        { class: "type-body text-secondary mb-4" },
        "You'll paste this into the chat of the machine you want to add this Template to.",
      ),
      m(
        "div",
        {
          class:
            "copy-box flex gap-2 items-center bg-fill-subtle border border-default rounded-md px-3 py-2 " +
            "cursor-pointer transition-colors",
          style: isMessageCopied
            ? "border-color: var(--c-success); background-color: var(--c-success-surface);"
            : "",
          onclick: () => doCopy(),
        },
        [
          m("input", {
            type: "text",
            readonly: true,
            value: `${ADOPT_COMMAND} ${gitUrl}`,
            onclick: (event: Event) => (event.target as HTMLInputElement).select(),
            class: "flex-1 bg-transparent border-0 type-body text-primary font-mono outline-none",
          }),
          m(
            Button,
            {
              variant: "ghost",
              extra: "shrink-0 transition-colors duration-300",
              // The copy confirmation: the button goes solid green with white
              // "Copied" (inline style so it always wins and stays theme-aware).
              style: isMessageCopied ? "background-color: var(--c-success); color: #ffffff;" : "",
            },
            isMessageCopied ? "Copied" : "Copy",
          ),
        ],
      ),
      branchName
        ? m(
            "p",
            { class: "type-helper text-tertiary mt-2" },
            `The link's branch (${branchName}) applies only when creating a new machine; the message above always uses the Template's published version.`,
          )
        : null,
    ]);
  }

  function addPickBody(): m.Children {
    if (isModal()) {
      return m("div", [
        m(
          "p",
          { class: "type-body text-secondary" },
          `The message is on your clipboard — paste it into ${machineName()}'s chat to get started.`,
        ),
        m(
          "div",
          { class: "mt-4" },
          m(
            Button,
            { variant: "secondary", block: true, onclick: () => getAppContext().shell.closeAppOverlay() },
            "Done",
          ),
        ),
      ]);
    }
    const machines = getAppContext().stores.workspaces.workspaces.filter(
      (entry) => !(entry.is_remote ?? false) && (entry.create_attempt_state ?? "") === "",
    );
    return m("div", [
      m(
        "p",
        { class: "type-body text-secondary mb-4" },
        "It will open so you can paste the copied message into its chat.",
      ),
      machines.length > 0
        ? m("div", { class: "flex flex-col gap-1.5" }, machines.map((entry) => machineRow(entry)))
        : m("p", { class: "type-body text-secondary" }, [
            "You don't have any machines yet. ",
            m(
              "a",
              {
                href: "#",
                class: "text-accent hover:underline",
                onclick: (event: Event) => {
                  event.preventDefault();
                  chooseBranch("create");
                },
              },
              "Create a new one from this Template",
            ),
            " instead.",
          ]),
    ]);
  }

  function buildSteps(): StepSpec[] {
    const steps: StepSpec[] = [];
    const branchSummary =
      branch === "create"
        ? "Create a new machine"
        : branch === "add"
          ? isModal()
            ? `Add to ${machineName()}`
            : "Add to an existing machine"
          : "";
    steps.push({
      number: 1,
      title: "What would you like to do with it?",
      target: 1,
      active: activeStep === 1,
      done: activeStep > 1,
      summary: branchSummary,
      body: step1Body,
    });
    if (branch === "create") {
      const presetSummary =
        model.selectedPreset === "remote"
          ? "Imbue Cloud"
          : model.selectedPreset === "local"
            ? "Directly on your computer"
            : "";
      if (activeStep >= 2)
        steps.push({
          number: 2,
          title: "Where should it run?",
          target: 2,
          active: activeStep === 2,
          done: activeStep > 2,
          summary: presetSummary,
          body: createPresetBody,
        });
      if (activeStep >= 3)
        steps.push({
          number: 3,
          title: "Confirm and create",
          target: 3,
          active: activeStep === 3,
          done: false,
          summary: "",
          body: createConfirmBody,
        });
    } else if (branch === "add") {
      if (activeStep >= 2)
        steps.push({
          number: 2,
          title: "Copy the message",
          target: 2,
          active: activeStep === 2,
          done: activeStep > 2,
          summary: "Message copied",
          body: addCopyBody,
        });
      if (activeStep >= 3)
        steps.push({
          number: 3,
          title: isModal() ? "Paste it into the chat" : "Select a machine",
          target: 3,
          active: activeStep === 3,
          done: false,
          summary: "",
          body: addPickBody,
        });
    }
    return steps;
  }

  function renderStep(spec: StepSpec, isLastVisible: boolean): m.Children {
    const state = spec.active ? "active" : spec.done ? "done" : "idle";
    const connector = !isLastVisible
      ? m("span", { class: "absolute left-1/2 -translate-x-1/2 top-4 bottom-0 step-connector-line" })
      : activeStep < 3
        ? m("span", { class: "absolute left-1/2 -translate-x-1/2 step-connector-dots" })
        : null;
    return m("div", { class: "flex gap-3" }, [
      m("div", { class: "relative flex flex-col items-center self-stretch" }, [
        m(
          "span",
          {
            "data-state": state,
            class:
              "template-step-circle relative z-10 flex items-center justify-center w-8 h-8 rounded-full " +
              "type-body font-semibold shrink-0 transition-colors duration-300" +
              (spec.done ? " cursor-pointer" : ""),
            onclick: spec.done ? () => goBack(spec.target) : undefined,
            oncreate: () => {
              if (spec.done) poppedSteps.add(spec.number);
            },
            onupdate: (vnode: m.VnodeDOM) => {
              if (spec.done && !poppedSteps.has(spec.number)) {
                poppedSteps.add(spec.number);
                animatePop(vnode.dom);
              } else if (!spec.done) {
                poppedSteps.delete(spec.number);
              }
            },
          },
          String(spec.number),
        ),
        connector,
      ]),
      m("div", { class: "flex-1 pb-6 min-w-0" }, [
        m(
          "h2",
          {
            class:
              "type-heading flex items-center min-h-8 " +
              (spec.done ? "text-secondary cursor-pointer hover:underline" : "text-primary"),
            onclick: spec.done ? () => goBack(spec.target) : undefined,
          },
          spec.title,
        ),
        spec.done && spec.summary
          ? m(
              "div",
              { class: "mt-0.5 mb-2" },
              m("span", { class: "flex items-center gap-2 type-body text-secondary" }, [
                m("span", { class: "text-success shrink-0" }, m(Icon16, { name: "check" })),
                spec.summary,
              ]),
            )
          : null,
        spec.active
          ? m("div", { class: "mt-4", oncreate: (vnode: m.VnodeDOM) => animateReveal(vnode.dom, 16, 520) }, spec.body())
          : null,
      ]),
    ]);
  }

  return {
    oninit() {
      // Defaults do not depend on the route, so load them once here; the flow
      // reads route params each render via syncRoute.
      fetchCreateFormDefaults(null)
        .then((defaults) => {
          model.applyDefaults(defaults);
          model.selectedPreset = null; // the stepper offers no default preset
          m.redraw();
        })
        .catch(() => m.redraw());
    },
    onremove() {
      for (const timer of copyTimers) clearTimeout(timer);
    },
    view() {
      if (!syncRoute()) {
        // No repo to inspire from -> the plain create form is the right surface.
        m.route.set("/create");
        return null;
      }
      const steps = buildSteps();
      const body = [
        m("div", { id: "template-content-top", class: "text-center mb-8" }, [
          m("h1", { class: "type-heading-lg text-primary" }, "You've opened a Template"),
          m("p", { class: "type-body text-secondary font-mono mt-2 break-all" }, gitUrl),
        ]),
        m(
          "div",
          steps.map((spec, index) => renderStep(spec, index === steps.length - 1)),
        ),
      ];
      // The modal renders bare inside the Shell's app-overlay card (padding +
      // scroll live there); the full page uses the narrow page container.
      return isModal()
        ? m("div", body)
        : m(PageNarrowContainer, { padding: "form", maxWidth: "max-w-[720px]" }, body);
    },
  };
};
