// A machine's size as the create form and the workspace settings page share
// it: the numbers a provider reports or takes, the text draft a user edits
// them through, and the one reading / one spelling of both.

/** A size given in numbers; a null dimension is unknown, or one the mode does not take. */
export interface MachineSizeNumbers {
  cpu_count: number | null;
  memory_gb: number | null;
  disk_gb: number | null;
}

/** One dimension of a machine size given in numbers. */
export type MachineSizeDimension = keyof MachineSizeNumbers;

export const MACHINE_SIZE_DIMENSIONS: MachineSizeDimension[] = ["cpu_count", "memory_gb", "disk_gb"];

/** Size fields as typed, so a half-typed value survives a redraw; "" is a dimension left alone. */
export interface MachineSizeDraft {
  cpu_count: string;
  memory_gb: string;
  disk_gb: string;
}

/**
 * "4 CPUs · 8 GB RAM · 100 GB disk"; "" when no dimension is known. A
 * provider reports an uncapped dimension as the machine's own total (docker:
 * MemTotal in GiB, a long fraction), so GB are shown to one decimal.
 */
export function formatMachineSizeNumbers(size: MachineSizeNumbers): string {
  const gb = (value: number): string => String(Math.round(value * 10) / 10);
  const parts: string[] = [];
  if (size.cpu_count !== null) parts.push(`${size.cpu_count} ${size.cpu_count === 1 ? "CPU" : "CPUs"}`);
  if (size.memory_gb !== null) parts.push(`${gb(size.memory_gb)} GB RAM`);
  if (size.disk_gb !== null) parts.push(`${gb(size.disk_gb)} GB disk`);
  return parts.join(" · ");
}

/** A typed size as a whole positive number, or null for anything else (blank, zero, a fraction, letters). */
export function parseMachineSizeNumber(text: string): number | null {
  const trimmed = text.trim();
  if (!/^[0-9]+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return value >= 1 ? value : null;
}

/** The size fields pre-filled from a size in numbers, as whole numbers; an unknown dimension is left blank. */
export function machineSizeDraftFor(size: MachineSizeNumbers): MachineSizeDraft {
  const whole = (value: number | null): string => (value === null ? "" : String(Math.round(value)));
  return { cpu_count: whole(size.cpu_count), memory_gb: whole(size.memory_gb), disk_gb: whole(size.disk_gb) };
}
