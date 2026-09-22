import { describe, expect, it, vi } from "vitest";
import { NotificationsUiController } from "../../models/notificationsUi";
import {
  allText,
  attrsOf,
  collectVnodes,
  renderRoot,
  renderedText,
} from "../../testing";
import { NotificationChoiceCard } from "./NotificationChoiceCard";

describe("notification choice card", () => {
  it("explains the default and saves the choice behind each button", () => {
    const controller = new NotificationsUiController({
      isFeedOverlayOpen: () => false,
    });
    const choose = vi
      .spyOn(controller, "chooseNotificationStyle")
      .mockResolvedValue();
    const root = renderRoot(NotificationChoiceCard, { controller });
    expect(renderedText(root)).toContain(
      "in-app cards and system banners by default",
    );
    const controls = collectVnodes(root).filter(
      (vnode) => typeof attrsOf(vnode).onclick === "function",
    );
    for (const [label, style] of [
      ["Both", "both"],
      ["In-app cards", "cards"],
      ["System notifications", "os"],
      ["Only the bell", null],
    ]) {
      const control = controls.find((vnode) =>
        allText(vnode.children).includes(label!),
      );
      expect(control).toBeDefined();
      (attrsOf(control!).onclick as () => void)();
      expect(choose).toHaveBeenLastCalledWith(style);
    }
  });

  it("disables choices while saving and shows an unsuccessful save", () => {
    const controller = new NotificationsUiController({
      isFeedOverlayOpen: () => false,
    });
    controller.isSavingChoice = true;
    controller.choiceError = "Could not save your choice. Please try again.";
    const root = renderRoot(NotificationChoiceCard, { controller });
    const controls = collectVnodes(root).filter(
      (vnode) => typeof attrsOf(vnode).onclick === "function",
    );
    expect(controls).toHaveLength(4);
    for (const control of controls)
      expect(attrsOf(control).disabled).toBe(true);
    expect(renderedText(root)).toContain(controller.choiceError);
  });
});
