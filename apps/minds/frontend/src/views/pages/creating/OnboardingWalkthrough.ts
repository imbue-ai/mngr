// Creating-page onboarding walkthrough: nine steps that play themselves
// while the machine is created, each swapping the graphic above the copy.
// Port of Creating.jinja's #onboarding block + onboarding.js.
//
// Every "replay this scene from scratch on revisit" behaviour (the chat
// retyping, the connections/sharing/publishing sequences, the app-cloud
// wheel, the dashboard's chart pop-in, the dot's fill animation) is driven
// by giving the relevant vnode a fresh `key` on every navigation rather
// than the legacy DOM version's remove-class/force-reflow/re-add-class
// dance: a freshly created node always starts its CSS animations from the
// beginning, in every browser, so keying is both simpler and more robust
// than replaying that dance against a persistent node. See models/walkthrough.ts
// for the four small timer-driven classes (WalkthroughStepper, ChatTypewriter,
// TipsRotator, CloudWheel) this component orchestrates.
import m from "mithril";
import type { OnboardingCloudApp } from "../../../models/create";
import type { WheelApp } from "../../../models/walkthrough";
import {
  ChatTypewriter,
  CloudWheel,
  LAST_STEP,
  TOTAL_STEPS,
  TipsRotator,
  WalkthroughStepper,
  dwellForStep,
  graphicForStep,
} from "../../../models/walkthrough";
import {
  appsGraphic,
  browserGraphic,
  chatGraphic,
  connectGraphic,
  devicesGraphic,
  machineGraphic,
  mindsGraphic,
  publishGraphic,
  tipsCopyPanel,
  tipsGraphic,
} from "./graphics";
import { Symbols } from "./symbols";

/**
 * The height every scene is drawn at, and so the height a full-size box is:
 * the scale is the box's real height as a fraction of this. Must match the
 * `max-h-[340px]` on #graphic below, which is spelled out because Tailwind
 * only generates utilities it can find as literal text in the source.
 */
const GRAPHIC_DESIGN_HEIGHT_PX = 340;

export interface OnboardingWalkthroughAttrs {
  isRemote: boolean;
  onboardingServices: OnboardingCloudApp[];
  /** True once the create attempt is done and a redirect is ready. */
  isReady: boolean;
  /** Called exactly once, when the walkthrough decides it is time to enter the workspace. */
  onEnter: () => void;
}

interface StepCopy {
  headline: string;
  body: string | ((isRemote: boolean) => string);
}

const STEP_COPY: Record<number, StepCopy> = {
  1: {
    headline: "Minds is your personal AI operating system.",
    body: "Learn your way around while your machine sets up.",
  },
  2: {
    headline: "Your machine is where you create with agents.",
    // Where it runs is decided at create time, so the page can say so.
    body: (isRemote) =>
      isRemote ? "This one runs in Imbue’s secure cloud, but is dedicated to you." : "This one runs right on your own computer.",
  },
  3: {
    headline: "Agents can help you make personal tools.",
    body: "Describe what you need, and your agents will make you apps to get it done.",
  },
  4: {
    headline: "Agents and tools run in tabs.",
    body: "Get your TODOs done, clear out your email, make a dashboard.",
  },
  5: {
    headline: "Agents can get data from your other apps.",
    body: "Connect to Slack, Notion, Gmail, or browse the web to complete tasks.",
  },
  6: {
    headline: "Your credentials remain safe.",
    body: "Agents can only perform actions you approve.",
  },
  7: {
    headline: "Share access with your teammates, friends, or even your phone.",
    // Reaching it with the laptop shut only holds for a machine in the
    // cloud; a local one is only up while this computer is.
    body: (isRemote) =>
      isRemote
        ? "This one runs in the cloud, so it is there even when your laptop is closed."
        : "This one runs locally, so your computer has to be on.",
  },
  8: {
    headline: "You can publish your apps, or adapt what others have made.",
    body: "Find others’ apps in the Templates catalog here.",
  },
};

function copyPanel(step: number, isRemote: boolean, tipText: string, tipOpacity: 0 | 1): m.Children {
  if (step === LAST_STEP) {
    return m("div", { class: "onboarding-step text-center flex flex-col items-center gap-3" }, tipsCopyPanel(tipText, tipOpacity));
  }
  const copy = STEP_COPY[step];
  const body = typeof copy.body === "function" ? copy.body(isRemote) : copy.body;
  return m("div", { class: "onboarding-step text-center flex flex-col items-center gap-1" }, [
    m("p", { class: "type-heading-lg text-primary max-w-xl" }, copy.headline),
    m("p", { class: "onboarding-copy text-primary max-w-xl" }, body),
  ]);
}

/**
 * One dot per step, in fixed-width slots so the strip never reflows; the
 * current step's dot stretches into a pill whose fill runs out over the
 * step's dwell. The dots themselves keep a stable identity across renders
 * (key: n) so only the current dot's fill span remounts -- keyed by
 * visitId, so re-clicking an already-current dot still restarts its fill.
 */
function dotsNav(stepper: WalkthroughStepper): m.Children {
  const dots: m.Children[] = [];
  for (let n = 1; n <= TOTAL_STEPS; n++) {
    const isCurrent = n === stepper.step;
    const isRunning = isCurrent && stepper.step !== LAST_STEP;
    dots.push(
      m(
        "button",
        {
          key: n,
          type: "button",
          class: "onboarding-dot" + (isCurrent ? " is-current" : "") + (isRunning ? " is-running" : ""),
          "data-dot": n,
          "aria-label": `Step ${n}`,
          onclick: () => stepper.goToStep(n),
        },
        m(
          "span",
          { class: "onboarding-dot-shape", "aria-hidden": "true" },
          m("span", {
            key: isRunning ? `fill-${stepper.visitId}` : "fill",
            class: "onboarding-dot-fill",
            style: isRunning ? `animation-duration: ${dwellForStep(stepper.step)}ms` : undefined,
          }),
        ),
      ),
    );
  }
  return m("nav", { id: "onboarding-dots", class: "onboarding-dots", "aria-label": "Walkthrough" }, dots);
}

export const OnboardingWalkthrough: m.ClosureComponent<OnboardingWalkthroughAttrs> = (initialVnode) => {
  const wheelApps: WheelApp[] = initialVnode.attrs.onboardingServices.map((service) => ({ url: service.icon, name: service.name }));

  const redraw = (): void => m.redraw();
  const stepper = new WalkthroughStepper(redraw);
  const chat = new ChatTypewriter(redraw);
  const tips = new TipsRotator(redraw);
  const appsWheel = new CloudWheel(wheelApps, redraw);
  const connectWheel = new CloudWheel(wheelApps, redraw);

  let isEntering = false;
  let hasTriggeredEntry = false;
  let graphicResizeObserver: ResizeObserver | null = null;
  // 0 never matches a real step number, so the first sync always runs.
  let lastSyncedStep = 0;

  // Only the current step's own timer should run; start()/stop() on the
  // other three are idempotent no-ops when already in that state, but
  // CloudWheel.start() always resets, so this must run only when the step
  // actually changed, not on every unrelated redraw.
  function syncStepTimers(): void {
    if (stepper.step === lastSyncedStep) return;
    lastSyncedStep = stepper.step;
    const graphic = graphicForStep(stepper.step);
    if (graphic === "gfx-chat") chat.start();
    else chat.stop();
    if (graphic === "gfx-tips") tips.start();
    else tips.stop();
    if (graphic === "gfx-apps") appsWheel.start();
    else appsWheel.stop();
    if (graphic === "gfx-connect") connectWheel.start();
    else connectWheel.stop();
  }

  /**
   * Keep the picture at whatever fraction of its design height the box came
   * out at. Each scene is laid out at fixed pixel sizes and scales as a whole
   * rather than reflowing into a shape it was not drawn for, so the one thing
   * the layout can give the page is this scale: the column above claims the
   * height it needs (the logs panel most of all), the box gets what is left,
   * and the illustration is drawn to fit it. Setting the scale changes no
   * layout -- a transform does not -- so this cannot feed back into a resize.
   */
  function trackGraphicScale(box: HTMLElement): void {
    const applyScale = (): void => {
      const scale = Math.min(1, box.clientHeight / GRAPHIC_DESIGN_HEIGHT_PX);
      box.style.setProperty("--gfx-scale", String(scale));
    };
    applyScale();
    graphicResizeObserver = new ResizeObserver(applyScale);
    graphicResizeObserver.observe(box);
  }

  // The workspace being ready wins over whatever step is showing: go in.
  // The zoom dives into the picture on screen, so it needs one; the last
  // step is a line of text with no illustration, and that is where the
  // walkthrough usually is by the time a machine is ready.
  function syncReadiness(attrs: OnboardingWalkthroughAttrs): void {
    if (!attrs.isReady || hasTriggeredEntry) return;
    hasTriggeredEntry = true;
    if (graphicForStep(stepper.step) === "gfx-tips") {
      attrs.onEnter();
      return;
    }
    isEntering = true;
    redraw();
    setTimeout(() => attrs.onEnter(), 650);
  }

  return {
    oninit(vnode) {
      syncStepTimers();
      stepper.start();
      syncReadiness(vnode.attrs);
    },
    onupdate(vnode) {
      syncStepTimers();
      syncReadiness(vnode.attrs);
    },
    onremove() {
      stepper.stop();
      chat.stop();
      tips.stop();
      appsWheel.stop();
      connectWheel.stop();
      graphicResizeObserver?.disconnect();
    },
    view(vnode) {
      const { isRemote } = vnode.attrs;
      const step = stepper.step;
      const graphicKey = `${step}-${stepper.visitId}`;
      const graphic = graphicForStep(step);
      let scene: m.Children;
      switch (graphic) {
        case "gfx-minds":
          scene = mindsGraphic(graphicKey);
          break;
        case "gfx-machine":
          scene = machineGraphic(graphicKey, isRemote);
          break;
        case "gfx-chat":
          scene = chatGraphic(graphicKey, chat.text);
          break;
        case "gfx-browser":
          scene = browserGraphic(graphicKey, stepper.arrivedFrom === 3);
          break;
        case "gfx-apps":
          scene = appsGraphic(graphicKey, appsWheel);
          break;
        case "gfx-connect":
          scene = connectGraphic(graphicKey, connectWheel);
          break;
        case "gfx-devices":
          scene = devicesGraphic(graphicKey);
          break;
        case "gfx-publish":
          scene = publishGraphic(graphicKey);
          break;
        case "gfx-tips":
          scene = tipsGraphic(graphicKey);
          break;
      }
      return m(
        "div",
        {
          id: "onboarding",
          // min-h-0 so this column can be shorter than the picture it holds:
          // it takes the height the progress block above leaves it, and the
          // graphic box below gives up the difference.
          class:
            "onboarding flex-1 min-h-0 flex flex-col items-center justify-center w-full max-w-3xl mx-auto px-6 py-6 gap-6" +
            (isEntering ? " is-entering" : ""),
          "data-step": String(step),
        },
        [
          m(Symbols),
          // The step text and dot strip below keep their own height, so the
          // box is the one thing that flexes -- capped at the height the
          // scenes are drawn at, since a picture never grows past full size.
          // It also clips: the scale is applied a frame after the box resizes,
          // and that frame must not reach the page as a flash of scrollbar.
          m(
            "div",
            {
              id: "graphic",
              class: "flex items-center justify-center w-full flex-1 min-h-0 max-h-[340px] overflow-hidden",
              oncreate: (graphicVnode) => trackGraphicScale(graphicVnode.dom as HTMLElement),
            },
            scene,
          ),
          copyPanel(step, isRemote, tips.text, tips.opacity),
          dotsNav(stepper),
        ],
      );
    },
  };
};
