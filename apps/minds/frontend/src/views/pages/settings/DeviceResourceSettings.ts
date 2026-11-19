// Studio settings' This computer section: resource warnings. Each of disk, memory and CPU shows its current use
// above the switch and level for its warning; the titlebar's warning pill opens this section. Every change is saved
// at once, like the section's other switches; a level is saved when its field is left. The readings refresh every
// few seconds while the section is open.

import m from "mithril";
import {
  alertOf,
  DEFAULT_DISK_WARNING_FREE_GB,
  describeUsage,
  MAX_DISK_WARNING_FREE_GB,
  MAX_WARNING_PERCENT,
  MIN_DISK_WARNING_FREE_GB,
  MIN_WARNING_PERCENT,
  type DeviceResourceSettings,
  type DeviceResourcesStore,
  type DeviceResourceView,
  type GaugeResource,
} from "../../../models/deviceResources";
import { TextInput } from "../../components/FormControls";
import { renderSwitch } from "../../components/Switch";

/** The level a warning switched on starts at. */
const DEFAULT_SHARE_WARNING_PERCENT = 90;

type LevelKey = "diskWarningFreeGb" | "memoryWarningPercent" | "cpuWarningPercent";

interface LevelSpec {
  readonly key: LevelKey;
  readonly resource: GaugeResource;
  readonly id: string;
  readonly title: string;
  readonly hint: string;
  readonly unit: string;
  readonly min: number;
  readonly max: number;
  readonly step: string;
  readonly initial: number;
}

const LEVELS: readonly LevelSpec[] = [
  {
    key: "diskWarningFreeGb",
    resource: "disk",
    id: "device-warning-disk",
    title: "Warn when disk space is low",
    hint: "Also warns when the disk is on course to fill within 15 minutes.",
    unit: "GB left",
    min: MIN_DISK_WARNING_FREE_GB,
    max: MAX_DISK_WARNING_FREE_GB,
    step: "0.5",
    initial: DEFAULT_DISK_WARNING_FREE_GB,
  },
  {
    key: "memoryWarningPercent",
    resource: "memory",
    id: "device-warning-memory",
    title: "Warn when memory is nearly full",
    hint: "",
    unit: "% in use",
    min: MIN_WARNING_PERCENT,
    max: MAX_WARNING_PERCENT,
    step: "1",
    initial: DEFAULT_SHARE_WARNING_PERCENT,
  },
  {
    key: "cpuWarningPercent",
    resource: "cpu",
    id: "device-warning-cpu",
    title: "Warn when the CPU is busy",
    hint: "Averaged over two minutes.",
    unit: "% in use",
    min: MIN_WARNING_PERCENT,
    max: MAX_WARNING_PERCENT,
    step: "1",
    initial: DEFAULT_SHARE_WARNING_PERCENT,
  },
];

export interface DeviceResourceSettingsBlockAttrs {
  store: DeviceResourcesStore;
}

export function DeviceResourceSettingsBlock(): m.Component<DeviceResourceSettingsBlockAttrs> {
  let isSaving = false;
  let error = "";

  async function save(store: DeviceResourcesStore, settings: DeviceResourceSettings): Promise<void> {
    isSaving = true;
    error = "";
    try {
      await store.saveSettings(settings, () => m.redraw());
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      isSaving = false;
      m.redraw();
    }
  }

  function levelRow(store: DeviceResourcesStore, view: DeviceResourceView, spec: LevelSpec): m.Children {
    const settings = view.settings;
    const level = settings[spec.key];
    const alert = alertOf(view, spec.resource);
    const isOn = level !== null;
    return m(
      "div",
      {
        class: "flex items-start justify-between gap-3 py-3 border-b border-subtle",
        "data-device-warning-level": spec.key,
      },
      [
        m("span", { class: "flex flex-col gap-1" }, [
          m(
            "span",
            { class: "type-helper text-secondary", "data-device-usage": spec.resource },
            describeUsage(view.readings, spec.resource),
          ),
          m("span", { class: "type-body text-primary font-semibold", id: `${spec.id}-label` }, spec.title),
          alert === null
            ? null
            : m(
                "span",
                {
                  class: alert.severity === "critical" ? "type-helper font-medium text-important" : "type-helper font-medium text-warning",
                  "data-device-alert": spec.resource,
                },
                `! ${alert.headline}`,
              ),
          spec.hint === "" ? null : m("span", { class: "type-helper text-tertiary" }, spec.hint),
          isOn
            ? m("span", { class: "flex items-center gap-2" }, [
                m(TextInput, {
                  id: `${spec.id}-level`,
                  name: spec.key,
                  type: "number",
                  min: String(spec.min),
                  max: String(spec.max),
                  step: spec.step,
                  value: String(level),
                  width: "w-20",
                  disabled: isSaving,
                  "aria-labelledby": `${spec.id}-label`,
                  onchange: (event: Event) => {
                    const value = Number((event.target as HTMLInputElement).value);
                    if (!Number.isFinite(value) || value < spec.min || value > spec.max) {
                      error = `Enter a number from ${spec.min} to ${spec.max}.`;
                      return;
                    }
                    void save(store, { ...settings, [spec.key]: value });
                  },
                }),
                m("span", { class: "type-helper text-secondary" }, spec.unit),
              ])
            : null,
        ]),
        renderSwitch({
          id: `${spec.id}-switch`,
          isOn,
          label: spec.title,
          isWaiting: isSaving,
          onFlip: (next) => void save(store, { ...settings, [spec.key]: next ? spec.initial : null }),
        }),
      ],
    );
  }

  return {
    oncreate(vnode) {
      vnode.attrs.store.startLiveRefresh(() => m.redraw());
    },
    onremove(vnode) {
      vnode.attrs.store.stopLiveRefresh();
    },
    view(vnode) {
      const { store } = vnode.attrs;
      const view = store.view;
      if (view === null) return null;
      return m("div", { id: "device-resource-settings", class: "mt-6" }, [
        m("h3", { class: "type-heading text-primary mb-1" }, "Resource warnings"),
        m(
          "p",
          { class: "type-helper text-tertiary mb-1" },
          "Warns in the titlebar when this computer runs low.",
        ),
        LEVELS.map((spec) => levelRow(store, view, spec)),
        error === "" ? null : m("p", { class: "type-body text-important mt-3", role: "alert" }, error),
      ]);
    },
  };
}
