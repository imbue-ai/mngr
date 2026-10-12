import m from "mithril";
import { Spinner } from "./Spinner";
import { renderSwitch } from "./Switch";

export interface PermissionSwitchAttrs {
  isOn: boolean;
  /** Its own write is in flight. */
  isBusy: boolean;
  /** Inert for a reason other than its own write. */
  isDisabled: boolean;
  /** Why it is inert, shown on hover. */
  title: string | null;
  label: string;
  /** Keys the switch's data attribute. */
  permission: string;
  /** Carries the state the flip asks for. */
  onFlip: (isOn: boolean) => void;
  /** Id of an element that says more than the label does. */
  describedBy?: string;
  /** Drawn between the spinner and the switch. */
  badge?: m.Children;
}

/** A permission switch, spinning while its own write runs.
 *
 * The write is not done until the workspace's own machine has taken it, so the
 * spinner is the honest state: the switch has not moved yet. */
export function renderPermissionSwitch(
  attrs: PermissionSwitchAttrs,
): m.Children {
  const { isBusy } = attrs;
  return m("span", { class: "flex shrink-0 items-center gap-2" }, [
    isBusy ? m(Spinner, { size: "sm" }) : null,
    attrs.badge ?? null,
    renderSwitch({
      isOn: attrs.isOn,
      label: attrs.label,
      onFlip: attrs.onFlip,
      wait: isBusy ? "busy" : null,
      isDisabled: attrs.isDisabled,
      title: attrs.title,
      describedBy: attrs.describedBy,
      data: { "data-perm-permission": attrs.permission },
    }),
  ]);
}
