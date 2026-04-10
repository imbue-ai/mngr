/**
 * Slot system for the plugin API. Slots are named extension points in the UI
 * that plugins can claim to replace default content with custom rendering.
 * Each slot can only be claimed once.
 *
 * When a slot is claimed with a render callback, the callback is automatically
 * re-invoked whenever the slot's DOM element is (re)created — for example
 * after returning from a plugin route that caused the App tree to be
 * destroyed and rebuilt.
 */

export type SlotRenderCallback = (container: HTMLElement) => void;

interface SlotRegistration {
  renderCallback: SlotRenderCallback | null;
}

const claimedSlots: Map<string, SlotRegistration> = new Map();

export function claimSlot(slotName: string, renderCallback?: SlotRenderCallback): boolean {
  if (claimedSlots.has(slotName)) {
    return false;
  }
  claimedSlots.set(slotName, { renderCallback: renderCallback ?? null });
  return true;
}

export function isSlotClaimed(slotName: string): boolean {
  return claimedSlots.has(slotName);
}

export function getSlotRenderCallback(slotName: string): SlotRenderCallback | null {
  return claimedSlots.get(slotName)?.renderCallback ?? null;
}
