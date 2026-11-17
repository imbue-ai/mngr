// A monogram stands in for a picture the app does not have: an owner with no
// profile picture, an app with no usable icon (specs/home-screen-redesign,
// 4.8). The letters come from the name, the colour from a hash of it, so the
// same name always wears the same tile.

/** The monogram grounds: eight dark hues that white letters read on. The first
 * two are the workspace palette's darkest (workspace_color.py); the rest are
 * chosen in the same register, since the palette's light entries would not
 * carry white text. */
export const MONOGRAM_COLORS: readonly string[] = [
  "#0b292b",
  "#492222",
  "#1f3d2b",
  "#2d2a4a",
  "#4a2d3f",
  "#3f3a1f",
  "#1f3a4a",
  "#4a3a2d",
];

/** The first letters of the first two words of `name`, upper-cased; "?" for an empty name. */
export function monogramLetters(name: string): string {
  const words = name
    .trim()
    .split(/[\s_-]+/)
    .filter((word) => word !== "");
  if (words.length === 0) return "?";
  return words
    .slice(0, 2)
    .map((word) => word[0].toUpperCase())
    .join("");
}

/** A stable colour for `seed`: the palette hue a small hash of the seed picks. */
export function monogramColor(seed: string): string {
  let hash = 0;
  for (const char of seed) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return MONOGRAM_COLORS[hash % MONOGRAM_COLORS.length];
}
