// Vendored from DitherKit (MIT): https://github.com/Boring-Software-Inc/dither-kit
// Shared seed palette for the dither chart family. Mirrors the seeds in
// `dither-chart.tsx` so a series rendered through the composable engine reads
// with the exact same fill / line / star hues as the legacy sparkline.

export type Rgb = [number, number, number]

export type DitherColor =
  | "blue"
  | "cyan"
  | "teal"
  | "green"
  | "mint"
  | "grey"

export type Seed = { fill: Rgb; line: Rgb; star: Rgb }

// Each seed: the area-fill hue, the bright series line, and the star sparkle.
export const PALETTE: Record<DitherColor, Seed> = {
  blue: { fill: [37, 99, 235], line: [147, 197, 253], star: [219, 234, 254] },
  cyan: { fill: [14, 165, 233], line: [125, 211, 252], star: [224, 242, 254] },
  teal: { fill: [13, 148, 136], line: [94, 234, 212], star: [204, 251, 241] },
  green: { fill: [22, 163, 74], line: [134, 239, 172], star: [220, 252, 231] },
  mint: { fill: [52, 211, 153], line: [167, 243, 208], star: [236, 253, 245] },
  // No-data remains neutral so empty metrics read as "nothing here".
  grey: { fill: [100, 116, 139], line: [148, 163, 184], star: [203, 213, 225] },
}

export const rgb = ([r, g, b]: Rgb, k = 1, a = 1) =>
  `rgba(${Math.round(r * k)},${Math.round(g * k)},${Math.round(b * k)},${a})`

export const seedOfColor = (color: DitherColor): Seed => PALETTE[color]

export const isDitherColor = (value: unknown): value is DitherColor =>
  typeof value === "string" && value in PALETTE
