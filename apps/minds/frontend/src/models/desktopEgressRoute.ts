// A service's desktop egress route: the ordered hops a workspace's machine
// tries when it sends the service a request. Pure helpers, so that every
// place a route is shown classifies, words and edits it the same way.
//
// A hop is a desktop's device id or SELF_HOP. A route is never empty, never
// repeats a hop, and SELF_HOP is only ever its last hop; every operation here
// keeps it that way.

import type { DesktopEgressMode, UiWorkspaceDesktop } from "../generated/ui";

export const SELF_HOP = "self";

/** The route of a service that does not go through a desktop at all. */
export const OFF_ROUTE: readonly string[] = [SELF_HOP];

export function areRoutesEqual(
  left: readonly string[],
  right: readonly string[],
): boolean {
  return (
    left.length === right.length &&
    left.every((hop, index) => hop === right[index])
  );
}

/** The device id of the computer the app runs on, or null when it is not known. */
export function thisComputerDeviceId(
  desktops: readonly UiWorkspaceDesktop[],
): string | null {
  return (
    desktops.find((desktop) => desktop.is_this_computer)?.device_id ?? null
  );
}

/** What the switch shows for a route: the two routes it sets itself, or custom.
 *
 * "On" is this computer alone, so the same route reads differently from
 * another computer. */
export function desktopEgressMode(
  route: readonly string[],
  thisDeviceId: string | null,
): DesktopEgressMode {
  if (areRoutesEqual(route, OFF_ROUTE)) return "OFF";
  if (thisDeviceId !== null && areRoutesEqual(route, [thisDeviceId]))
    return "ON";
  return "CUSTOM";
}

/** Whether a hop is shown by its device id rather than in words. */
export function isDesktopEgressHopShownById(
  hop: string,
  desktops: readonly UiWorkspaceDesktop[],
): boolean {
  return hop !== SELF_HOP && hop !== thisComputerDeviceId(desktops);
}

export function desktopEgressHopLabel(
  hop: string,
  desktops: readonly UiWorkspaceDesktop[],
): string {
  if (hop === SELF_HOP) return "The workspace's own machine";
  if (hop === thisComputerDeviceId(desktops)) return "This computer";
  return hop;
}

export interface DesktopEgressHopPhrase {
  text: string;
  /** The text is a device id, to be drawn as an identifier. */
  isDeviceId: boolean;
}

/** The hops in the order they are tried, each as it reads within one line:
 * words keep their capital only where they open the line, a device id always
 * keeps its own. `isMidSentence` says the first hop does not open the line. */
export function desktopEgressHopPhrases(
  route: readonly string[],
  desktops: readonly UiWorkspaceDesktop[],
  isMidSentence: boolean,
): DesktopEgressHopPhrase[] {
  return route.map((hop, index) => {
    const label = desktopEgressHopLabel(hop, desktops);
    const isDeviceId = isDesktopEgressHopShownById(hop, desktops);
    if (isDeviceId || (index === 0 && !isMidSentence))
      return { text: label, isDeviceId };
    return {
      text: label.charAt(0).toLowerCase() + label.slice(1),
      isDeviceId,
    };
  });
}

export const HOP_PHRASE_SEPARATOR = ", then ";

/** The sentence for a route the switch sets itself, or null for a custom one. */
export function desktopEgressModeSentence(
  mode: DesktopEgressMode,
): string | null {
  if (mode === "OFF") return "Requests leave from the workspace's own machine.";
  if (mode === "ON") return "Requests leave from this computer.";
  return null;
}

/** The route the switch sets when flipped, or null when it cannot be flipped
 * that way: turning it on names this computer, whose id may not be known. */
export function switchedDesktopEgressRoute(
  isOn: boolean,
  thisDeviceId: string | null,
): string[] | null {
  if (!isOn) return [...OFF_ROUTE];
  return thisDeviceId === null ? null : [thisDeviceId];
}

/** Every hop the route does not hold yet, in the order the editor offers them. */
export function addableDesktopEgressHops(
  route: readonly string[],
  desktops: readonly UiWorkspaceDesktop[],
): string[] {
  return [...desktops.map((desktop) => desktop.device_id), SELF_HOP].filter(
    (hop) => !route.includes(hop),
  );
}

/** Add a hop as the last thing tried, short of the workspace's own machine. */
export function addDesktopEgressHop(
  route: readonly string[],
  hop: string,
): string[] {
  if (route.includes(hop)) return [...route];
  if (route[route.length - 1] === SELF_HOP)
    return [...route.slice(0, -1), hop, SELF_HOP];
  return [...route, hop];
}

/** A route has to keep a hop: with none, requests would have nowhere to leave from. */
export function canRemoveDesktopEgressHop(route: readonly string[]): boolean {
  return route.length > 1;
}

export function removeDesktopEgressHop(
  route: readonly string[],
  index: number,
): string[] {
  if (!canRemoveDesktopEgressHop(route)) return [...route];
  return route.filter((_hop, position) => position !== index);
}

export function canMoveDesktopEgressHopUp(
  route: readonly string[],
  index: number,
): boolean {
  return index > 0 && index < route.length && route[index] !== SELF_HOP;
}

export function canMoveDesktopEgressHopDown(
  route: readonly string[],
  index: number,
): boolean {
  return (
    index >= 0 && index < route.length - 1 && route[index + 1] !== SELF_HOP
  );
}

function swapHops(
  route: readonly string[],
  first: number,
  second: number,
): string[] {
  const swapped = [...route];
  [swapped[first], swapped[second]] = [swapped[second], swapped[first]];
  return swapped;
}

export function moveDesktopEgressHopUp(
  route: readonly string[],
  index: number,
): string[] {
  return canMoveDesktopEgressHopUp(route, index)
    ? swapHops(route, index - 1, index)
    : [...route];
}

export function moveDesktopEgressHopDown(
  route: readonly string[],
  index: number,
): string[] {
  return canMoveDesktopEgressHopDown(route, index)
    ? swapHops(route, index, index + 1)
    : [...route];
}
