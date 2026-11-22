// A link as a QR code's modules, for the code Sharing draws beside a share
// link so a phone can open it.

import qrcode from "qrcode-generator";

/** The clear margin a QR code needs around it to be found by a camera, in modules. */
export const QR_QUIET_ZONE_MODULES = 4;

/** The dark modules of `text`'s QR code, row by row. Error correction level M: a link is short, and about 15% of
 * the code can be unreadable (a glare, a fold) before it stops scanning. */
export function qrModules(text: string): boolean[][] {
  // Type number 0 picks the smallest code that holds the text.
  const code = qrcode(0, "M");
  code.addData(text);
  code.make();
  const count = code.getModuleCount();
  return Array.from({ length: count }, (_unusedRow, row) =>
    Array.from({ length: count }, (_unusedColumn, column) => code.isDark(row, column)),
  );
}

/** The modules as one SVG path, a unit square per dark module, offset by the quiet zone. */
export function qrPathData(modules: readonly (readonly boolean[])[]): string {
  const segments: string[] = [];
  modules.forEach((row, rowIdx) => {
    row.forEach((isDark, columnIdx) => {
      if (isDark) segments.push(`M${columnIdx + QR_QUIET_ZONE_MODULES} ${rowIdx + QR_QUIET_ZONE_MODULES}h1v1h-1z`);
    });
  });
  return segments.join("");
}
