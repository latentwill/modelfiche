// @ts-expect-error -- The browser package intentionally omits Node types; Vitest executes this fixture in Node.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const themeCss = readFileSync("src/vela-theme.css", "utf8");

function declarations(selector: string, from = 0) {
  const marker = `${selector} {`;
  const start = themeCss.indexOf(marker, from);
  if (start < 0) throw new Error(`Missing CSS selector: ${selector}; loaded ${themeCss.length} characters starting ${JSON.stringify(themeCss.slice(0, 40))}`);
  const bodyStart = start + marker.length;
  const bodyEnd = themeCss.indexOf("}", bodyStart);
  if (bodyEnd < 0) throw new Error(`Unclosed CSS selector: ${selector}`);
  return Object.fromEntries(
    [...themeCss.slice(bodyStart, bodyEnd).matchAll(/([-\w]+)\s*:\s*([^;{}]+);/g)]
      .map(([, property, value]) => [property, value.trim()]),
  );
}

const lightTokens = declarations(".vela-theme");
const darkTokens = declarations('html[data-theme="dark"] .vela-theme');
const dropdownOption = declarations(".vela-dropdown-option");
const focusVisible = declarations(".vela-theme :is(button, a, input, select, textarea, summary, [tabindex]):focus-visible");
const reducedMotion = declarations(".vela-theme *, .vela-theme *::before, .vela-theme *::after", themeCss.indexOf("@media (prefers-reduced-motion: reduce)"));
const tableRegion = declarations(".vela-table-wrap");
const matrixRegion = declarations(".vela-theme .grid-board");
const mobileContainmentBreakpoint = 680;
const mobileShell = declarations(
  '.vela-shell, .vela-shell[data-nav-collapsed="true"]',
  themeCss.indexOf(`@media (max-width: ${mobileContainmentBreakpoint}px)`),
);

function rgb(value: string) {
  const hex = value.trim().match(/^#([\da-f]{6})$/i)?.[1];
  if (!hex) throw new Error(`Expected a six-digit color token, received ${value}`);
  return [0, 2, 4].map(offset => Number.parseInt(hex.slice(offset, offset + 2), 16));
}

function luminance(value: string) {
  const channels = rgb(value).map(channel => {
    const normalized = channel / 255;
    return normalized <= 0.04045 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrast(foreground: string, background: string) {
  const values = [luminance(foreground), luminance(background)].sort((left, right) => right - left);
  return (values[0] + 0.05) / (values[1] + 0.05);
}

function token(name: string, theme: string) {
  return (theme === "dark" ? darkTokens[name] : undefined) ?? lightTokens[name] ?? "";
}

describe("shared theme accessibility contracts", () => {
  it.each(["light", "dark"])("keeps representative %s theme text tokens at 4.5:1 or better", theme => {
    const surface = token("--mf-surface", theme);
    const pairs = [
      { name: "muted text on surface", foreground: token("--mf-ink-muted", theme), background: surface },
      { name: "accent text on surface", foreground: token("--mf-accent", theme), background: surface },
      { name: "danger text on surface", foreground: token("--mf-danger", theme), background: surface },
      { name: "active control text", foreground: token("--mf-active-text", theme), background: token("--mf-active-fill", theme) },
      ...(theme === "dark" ? [
        { name: "muted text on data fill", foreground: token("--mf-ink-muted", theme), background: token("--mf-data-fill", theme) },
        { name: "accent text on data fill", foreground: token("--mf-accent", theme), background: token("--mf-data-fill", theme) },
      ] : []),
    ];

    pairs.forEach(pair => expect(contrast(pair.foreground, pair.background), pair.name).toBeGreaterThanOrEqual(4.5));
  });

  it("uses one 44px target token for shared compact and option controls", () => {
    expect(token("--vela-touch-target", "light")).toBe("44px");
    expect(token("--vela-compact-control-mobile", "light")).toBe("var(--vela-touch-target)");
    expect(dropdownOption["min-height"]).toBe("var(--vela-touch-target)");
  });

  it("keeps keyboard focus visible for native and programmatically focusable controls", () => {
    expect(focusVisible.outline).toContain("var(--vela-accent)");
    expect(focusVisible["outline-offset"]).toBe("2px");
  });

  it("suppresses transitions, animation, and smooth scrolling for reduced motion", () => {
    expect(reducedMotion["scroll-behavior"]).toBe("auto !important");
    expect(reducedMotion.transition).toBe("none !important");
    expect(reducedMotion.animation).toBe("none !important");
  });

  it("contains responsive table and matrix overflow inside their named regions", () => {
    expect(tableRegion["overflow-x"]).toBe("auto");
    expect(tableRegion["overscroll-behavior-x"]).toBe("contain");
    expect(matrixRegion["max-width"]).toBe("100%");
    expect(matrixRegion.overflow).toBe("auto");
  });

  it.each([
    { label: "390px mobile", effectiveWidth: 390 },
    { label: "1280px viewport at 200% zoom", effectiveWidth: 640 },
  ])("keeps $label inside the single-column shell boundary", ({ effectiveWidth }) => {
    expect(effectiveWidth).toBeLessThanOrEqual(mobileContainmentBreakpoint);
    expect(mobileShell.display).toBe("block");
    expect(tableRegion["overflow-x"]).toBe("auto");
  });
});
