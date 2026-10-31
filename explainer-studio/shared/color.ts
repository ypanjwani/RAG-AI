// Custom mode: derive a full explainer palette (the variables a theme in
// themes.css defines) from one accent colour the reader picked. Pure code, so
// contrast is guaranteed by construction and checked in tests.

export type PaletteVars = Record<(typeof PALETTE_VARS)[number], string>;
export const PALETTE_VARS = ["accent", "accent-2", "accent-soft", "accent-text", "grad", "cat-0", "cat-1", "cat-2", "cat-3", "cat-4", "cat-5", "neg", "pos"] as const;

/** Surfaces from tokens.css that accent text must stay readable on. */
export const SURFACES = { light: ["#ffffff", "#f9f9f9"], dark: ["#212121", "#2a2a2a"] } as const;
const ON_ACCENT = "#ffffff";
const MIN_TEXT = 4.5;

export function isHex(s: unknown): s is string {
  return typeof s === "string" && /^#[0-9a-f]{6}$/i.test(s);
}

type Hsl = [number, number, number]; // h in degrees, s and l in 0..1

function hexToRgb(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function rgbToHsl([r, g, b]: [number, number, number]): Hsl {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  const h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return [h * 60, s, l];
}

export function hslToHex([h, s, l]: Hsl): string {
  h = ((h % 360) + 360) % 360;
  const k = (n: number) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return "#" + [f(0), f(8), f(4)].map((x) => Math.round(x * 255).toString(16).padStart(2, "0")).join("");
}

function luminance(hex: string): number {
  const c = hexToRgb(hex).map((v) => {
    const x = v / 255;
    return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}

/** WCAG contrast ratio between two #rrggbb colours. */
export function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}

/** Step lightness (down if dir < 0, up if > 0) until ok() holds or the end of the range. */
function shiftUntil([h, s, l]: Hsl, dir: number, ok: (hex: string) => boolean): string {
  for (let i = 0; i <= 100; i++) {
    const hex = hslToHex([h, s, Math.min(1, Math.max(0, l + dir * i * 0.01))]);
    if (ok(hex)) return hex;
  }
  return hslToHex([h, s, dir < 0 ? 0 : 1]);
}

const readableOn = (bgs: readonly string[], min: number) => (hex: string) => bgs.every((bg) => contrast(hex, bg) >= min);

function alpha(hex: string, a: number): string {
  const [r, g, b] = hexToRgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}

// Categorical colours: hue offsets from the accent, at fixed saturation and a
// lightness tuned for each surface, so even a grey accent gets a lively set.
const CAT_OFFSETS = [150, 210, 300, 60];

/** Light and dark palette variables for one accent colour. */
export function derivePalette(accentHex: string): { light: PaletteVars; dark: PaletteVars } {
  const base = rgbToHsl(hexToRgb(isHex(accentHex) ? accentHex : "#2563eb"));
  const [h, s] = base;
  const catS = Math.max(0.65, Math.min(s, 0.85));

  // White text (buttons, banners) must be readable on the accent.
  const accent = shiftUntil(base, -1, (x) => contrast(ON_ACCENT, x) >= MIN_TEXT);
  const [, , al] = rgbToHsl(hexToRgb(accent));
  const accent2 = shiftUntil([h + 35, s, al], -1, (x) => contrast(ON_ACCENT, x) >= MIN_TEXT);
  const deep = hslToHex([h + 70, s, Math.max(0.15, al - 0.12)]);
  const lightSoft = hslToHex([h, Math.min(s, 0.9), 0.96]);

  const cats = (l: number) => CAT_OFFSETS.map((o) => hslToHex([h + o, catS, l]));
  const lightCats = cats(0.42);
  const darkCats = cats(0.66);

  const light: PaletteVars = {
    "accent": accent,
    "accent-2": accent2,
    "accent-soft": lightSoft,
    "accent-text": shiftUntil(rgbToHsl(hexToRgb(accent)), -1, readableOn([...SURFACES.light, lightSoft], MIN_TEXT)),
    "grad": `linear-gradient(135deg, ${accent} 0%, ${accent2} 55%, ${deep} 100%)`,
    "cat-0": accent, "cat-1": lightCats[0], "cat-2": lightCats[1], "cat-3": lightCats[2], "cat-4": lightCats[3],
    "cat-5": hslToHex([h, 0.1, 0.45]),
    "neg": accent, "pos": lightCats[0],
  };

  const darkCat0 = shiftUntil(base, 1, readableOn(SURFACES.dark, 3));
  const dark: PaletteVars = {
    ...light,
    "accent-soft": alpha(accent, 0.14),
    "accent-text": shiftUntil(base, 1, readableOn(SURFACES.dark, MIN_TEXT)),
    "cat-0": darkCat0, "cat-1": darkCats[0], "cat-2": darkCats[1], "cat-3": darkCats[2], "cat-4": darkCats[3],
    "cat-5": hslToHex([h, 0.1, 0.66]),
    "neg": darkCat0, "pos": darkCats[0],
  };
  return { light, dark };
}
