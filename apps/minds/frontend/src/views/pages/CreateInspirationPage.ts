// Create from Inspiration (port of InspirationCreate.jinja): a deeplink
// lands here with ?git_url=...; the user either creates a new machine from
// the linked repo (same preset pair as the create form) or adds the
// Inspiration to an existing machine by copying the /use-inspiration message
// and picking a machine. Simplified from the legacy numbered stepper into
// two plain branches per the migration's simplification license.

import m from "mithril";
import { getAppContext } from "../../app-context";
import { fetchCreateFormDefaults, recoveryRoute } from "../../models/create";
import type { UiWorkspaceEntry } from "../../channel/messages";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { CopyField, PageNarrowContainer } from "../components/Layout";
import { Notice } from "../components/Notice";
import { PresetCards } from "./create/PresetCards";
import type { PresetName } from "./create/form-model";
import { CreateFormModel, normalizeCreateApiError } from "./create/form-model";

type InspirationBranch = "create" | "add" | null;

export const CreateInspirationPage: m.ClosureComponent = () => {
  const model = new CreateFormModel();
  let branch: InspirationBranch = null;
  let gitUrl = "";
  let branchName = "";
  let isMessageCopied = false;
  let submitError = "";

  function submitCreate(): void {
    model.gitUrl = gitUrl;
    model.branch = branchName;
    if (model.imbueCloudNeedsAccount()) {
      m.route.set("/accounts");
      return;
    }
    model.isSubmitting = true;
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

  async function copyInspirationMessage(): Promise<void> {
    // Actually write the message to the clipboard before claiming it was
    // copied (the CopyField click only selects the text). On failure the
    // selection stays, so the user can still copy manually.
    try {
      await navigator.clipboard.writeText(`/use-inspiration ${gitUrl}`);
    } catch {
      return;
    }
    isMessageCopied = true;
    m.redraw();
  }

  function machineRow(entry: UiWorkspaceEntry): m.Children {
    const { stores, shell } = getAppContext();
    const isStopped = (entry.supports_shutdown ?? false) && entry.liveness === "STOPPED";
    return m(
      Card,
      {
        layout: "row",
        interactive: true,
        extra: "accent-spine relative overflow-hidden cursor-pointer",
        style: `--workspace-accent: ${entry.accent};`,
        onclick: () => {
          if (isStopped) {
            const returnTo = `/goto/${stores.workspaces.toHostScopedId(entry.id)}/`;
            m.route.set(recoveryRoute(entry.id, returnTo, true));
          } else {
            shell.enterWorkspace(entry.id);
          }
        },
      },
      [
        m("span", { class: "flex-1 min-w-0 truncate font-semibold text-primary pl-1" }, entry.name),
        isStopped
          ? m(
              "span",
              { class: "inline-flex items-center px-2 py-0.5 rounded-md type-label bg-fill-subtle text-primary" },
              "Stopped",
            )
          : null,
      ],
    );
  }

  return {
    oninit() {
      gitUrl = m.route.param("git_url") ?? "";
      branchName = m.route.param("branch") ?? "";
      if (!gitUrl) {
        m.route.set("/create");
        return;
      }
      const start = m.route.param("start");
      if (start === "create" || start === "add") branch = start;
      fetchCreateFormDefaults(null)
        .then((defaults) => {
          model.applyDefaults(defaults);
          model.applyPreset("remote");
          m.redraw();
        })
        .catch(() => {
          model.loadError = "Could not load the create form. Please try again.";
          m.redraw();
        });
    },
    view() {
      const machines = getAppContext().stores.workspaces.workspaces.filter(
        (entry) => !(entry.is_remote ?? false) && (entry.create_attempt_state ?? "") === "",
      );
      return m(PageNarrowContainer, { padding: "form", maxWidth: "max-w-[720px]" }, [
        m("div", { class: "text-center mb-10" }, [
          m("p", { class: "type-label uppercase tracking-wide text-secondary" }, "Inspiration"),
          m("h1", { class: "type-heading-lg text-primary mt-1" }, "How do you want to use it?"),
          m("p", { class: "mt-2 type-helper text-tertiary break-all" }, gitUrl),
        ]),
        model.loadError ? m(Notice, { variant: "error", extra: "mb-6" }, model.loadError) : null,
        submitError ? m(Notice, { variant: "error", extra: "mb-6" }, submitError) : null,
        m("div", { class: "flex gap-4 justify-center mb-10" }, [
          m(
            Button,
            {
              variant: branch === "create" ? "primary" : "secondary",
              onclick: () => {
                branch = "create";
              },
            },
            "Create a new machine",
          ),
          m(
            Button,
            {
              variant: branch === "add" ? "primary" : "secondary",
              disabled: machines.length === 0,
              title: machines.length === 0 ? "No existing machines yet" : undefined,
              onclick: () => {
                branch = "add";
              },
            },
            "Add to an existing machine",
          ),
        ]),
        branch === "create"
          ? m("div", [
              m(
                "div",
                { role: "radiogroup", "aria-label": "Where to run your machine", class: "mb-8" },
                m(PresetCards, {
                  selectedPreset: model.selectedPreset,
                  onSelect: (name: PresetName) => model.applyPreset(name),
                }),
              ),
              m(
                "div",
                { class: "flex justify-center" },
                m(
                  Button,
                  { variant: "primary", extra: "w-80", disabled: model.isSubmitting, onclick: submitCreate },
                  model.isSubmitting ? "Creating..." : "Create",
                ),
              ),
            ])
          : null,
        branch === "add"
          ? m("div", { class: "flex flex-col gap-6" }, [
              m("div", [
                m(
                  "p",
                  { class: "type-body text-primary mb-2" },
                  "1. Copy this message (paste it into the machine's chat):",
                ),
                m(
                  "div",
                  { onclick: () => void copyInspirationMessage() },
                  m(CopyField, {
                    value: `/use-inspiration ${gitUrl}`,
                    id: "inspiration-skill-message",
                    "aria-label": "Inspiration chat message",
                  }),
                ),
              ]),
              m("div", [
                m(
                  "p",
                  { class: "type-body text-primary mb-2" },
                  isMessageCopied ? "2. Pick the machine to open:" : "2. Then pick the machine to open:",
                ),
                m("div", { class: "flex flex-col gap-1.5" }, machines.map((entry) => machineRow(entry))),
              ]),
            ])
          : null,
      ]);
    },
  };
};
