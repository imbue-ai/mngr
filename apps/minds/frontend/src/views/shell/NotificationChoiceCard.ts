import m from "mithril";
import type {
  NotificationsUiController,
  NotificationStyle,
} from "../../models/notificationsUi";
import { Button } from "../components/Button";

const CHOICES: { label: string; style: NotificationStyle | null }[] = [
  { label: "Both", style: "both" },
  { label: "In-app cards", style: "cards" },
  { label: "System notifications", style: "os" },
  { label: "Only the bell", style: null },
];

/** A standing choice after the first notification; leaves the workspace usable. */
export function NotificationChoiceCard(): m.Component<{
  controller: NotificationsUiController;
}> {
  return {
    view({ attrs: { controller } }) {
      return m(
        "section",
        {
          role: "region",
          "aria-label": "Choose notification style",
          class:
            "rounded-lg border border-subtle bg-surface-primary p-4 shadow-raised",
        },
        [
          m(
            "h2",
            { class: "type-label text-primary" },
            "How should Mind notify you?",
          ),
          m(
            "p",
            { class: "type-helper text-secondary mt-1 mb-3" },
            "You’ve received your first notification. We use in-app cards and system banners by default. Choose what you’d like from now on.",
          ),
          m(
            "div",
            { class: "flex flex-wrap gap-2" },
            CHOICES.map(({ label, style }) =>
              m(
                Button,
                {
                  variant: style === "both" ? "primary" : "secondary",
                  disabled: controller.isSavingChoice,
                  onclick: () => void controller.chooseNotificationStyle(style),
                },
                label,
              ),
            ),
          ),
          m(
            "p",
            { class: "type-helper text-tertiary mt-3" },
            "You can change this anytime in Settings → Notifications.",
          ),
          controller.choiceError !== ""
            ? m(
                "p",
                { role: "alert", class: "type-helper text-important mt-2" },
                controller.choiceError,
              )
            : null,
        ],
      );
    },
  };
}
