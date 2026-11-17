// The colour a home card's text sits on, read from the wallpaper itself
// (specs/home-screen-redesign, 4.12): the accent says nothing about an
// arbitrary image. The image is drawn cover-fitted onto a tiny canvas the
// card's shape, the top strip (where the name sits) is averaged, and the
// average is pushed well to one side: a clearly light strip gets a light
// tone (black text), anything else a dark one (white text). The card then
// re-bases its text on that colour through titlebar-surface and fades it in
// behind the name, so text and scrim come from the same pixels.
//
// Cached per image URL, failures included: null until the image has loaded,
// when the caller's redraw is asked for, and null for good once the image
// has failed to load or to sample (a later load of the workspace gives the
// wallpaper a new version, so a new URL). The images are same-origin (the
// desktop serves them), so the canvas is readable.

const WIDTH = 64;
const HEIGHT = 18;
/** The strip averaged: the top 45%, where the card's name row sits. */
const STRIP_HEIGHT = Math.round(HEIGHT * 0.45);
/** A strip at least this light (CIE L*) gets black text; the rest lean white. */
const LIGHT_LIGHTNESS = 68;

/** The tone per image URL, or null for an image that could not be read. */
const toneByUrl = new Map<string, string | null>();
const loadingUrls = new Set<string>();

/** CIE L* (0 to 100) of an sRGB colour with 0 to 255 channels. */
export function lightnessOf(r: number, g: number, b: number): number {
  const linear = (channel: number) => {
    const v = channel / 255;
    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  };
  const y = 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
  return y > 0.008856 ? 116 * Math.cbrt(y) - 16 : 903.3 * y;
}

/** The tone an averaged strip colour gives: pushed light for black text, dark for white text. */
export function toneFor(r: number, g: number, b: number): string {
  const base = `rgb(${Math.round(r)} ${Math.round(g)} ${Math.round(b)})`;
  return lightnessOf(r, g, b) >= LIGHT_LIGHTNESS
    ? `lch(from ${base} max(l, 85) c h)`
    : `lch(from ${base} min(l, 20) c h)`;
}

function sample(image: HTMLImageElement): string | null {
  const canvas = document.createElement("canvas");
  canvas.width = WIDTH;
  canvas.height = HEIGHT;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (context === null) return null;
  const imageWidth = image.naturalWidth;
  const imageHeight = image.naturalHeight;
  if (imageWidth === 0 || imageHeight === 0) return null;
  const scale = Math.max(WIDTH / imageWidth, HEIGHT / imageHeight);
  context.drawImage(
    image,
    (WIDTH - imageWidth * scale) / 2,
    (HEIGHT - imageHeight * scale) / 2,
    imageWidth * scale,
    imageHeight * scale,
  );
  const { data } = context.getImageData(0, 0, WIDTH, STRIP_HEIGHT);
  let r = 0;
  let g = 0;
  let b = 0;
  const pixelCount = data.length / 4;
  for (let i = 0; i < data.length; i += 4) {
    r += data[i];
    g += data[i + 1];
    b += data[i + 2];
  }
  return toneFor(r / pixelCount, g / pixelCount, b / pixelCount);
}

/**
 * The tone of the wallpaper at `url`, or null while it loads (the first ask
 * starts the load; `onLoaded` is called once a tone is known so the caller
 * can redraw). A URL whose image cannot be read stays null for good, without
 * another load.
 */
export function wallpaperToneFor(url: string, onLoaded: () => void): string | null {
  if (toneByUrl.has(url)) return toneByUrl.get(url) ?? null;
  if (loadingUrls.has(url) || typeof Image === "undefined") return null;
  loadingUrls.add(url);
  const image = new Image();
  image.onload = () => {
    const tone = sample(image);
    loadingUrls.delete(url);
    toneByUrl.set(url, tone);
    if (tone !== null) onLoaded();
  };
  image.onerror = () => {
    loadingUrls.delete(url);
    toneByUrl.set(url, null);
  };
  image.src = url;
  return null;
}
