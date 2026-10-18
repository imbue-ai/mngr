import { describe, expect, it } from "vitest";
import { formatMachineSizeNumbers, machineSizeDraftFor, parseMachineSizeNumber } from "./machineSize";

describe("formatMachineSizeNumbers", () => {
  it("renders the size from CPUs, memory and disk", () => {
    expect(formatMachineSizeNumbers({ cpu_count: 4, memory_gb: 8, disk_gb: 28 })).toBe("4 CPUs · 8 GB RAM · 28 GB disk");
    expect(formatMachineSizeNumbers({ cpu_count: 1, memory_gb: 8, disk_gb: 28 })).toBe("1 CPU · 8 GB RAM · 28 GB disk");
  });

  it("shows an uncapped dimension's long fraction to one decimal", () => {
    expect(formatMachineSizeNumbers({ cpu_count: 8, memory_gb: 15.52734375, disk_gb: 99.96 })).toBe(
      "8 CPUs · 15.5 GB RAM · 100 GB disk",
    );
  });

  it("omits the dimensions it does not know", () => {
    expect(formatMachineSizeNumbers({ cpu_count: null, memory_gb: 8, disk_gb: null })).toBe("8 GB RAM");
    expect(formatMachineSizeNumbers({ cpu_count: null, memory_gb: null, disk_gb: null })).toBe("");
  });
});

describe("parseMachineSizeNumber", () => {
  it("reads a whole positive number, with surrounding blanks", () => {
    expect(parseMachineSizeNumber("16")).toBe(16);
    expect(parseMachineSizeNumber(" 2 ")).toBe(2);
  });

  it("refuses anything else", () => {
    expect(parseMachineSizeNumber("")).toBeNull();
    expect(parseMachineSizeNumber("0")).toBeNull();
    expect(parseMachineSizeNumber("1.5")).toBeNull();
    expect(parseMachineSizeNumber("four")).toBeNull();
    expect(parseMachineSizeNumber("-2")).toBeNull();
  });
});

describe("machineSizeDraftFor", () => {
  it("pre-fills the fields with the size as whole numbers, leaving an unknown dimension blank", () => {
    expect(machineSizeDraftFor({ cpu_count: 4, memory_gb: 7.8, disk_gb: null })).toEqual({
      cpu_count: "4",
      memory_gb: "8",
      disk_gb: "",
    });
  });
});
