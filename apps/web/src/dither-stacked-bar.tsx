import { useLayoutEffect, useRef, useState } from "react";
import { BAYER, CELL } from "./components/dither-kit/dither-paint";
import { PALETTE, type DitherColor } from "./components/dither-kit/palette";

export type DitherBarSegment = {
  key: string;
  label: string;
  value: number;
  color: DitherColor;
};

export function DitherStackedBar({ segments, label }: { segments: DitherBarSegment[]; label: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const host = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const total = segments.reduce((sum, segment) => sum + Math.max(0, segment.value), 0);

  useLayoutEffect(() => {
    const element = host.current;
    if (!element) return;
    if (typeof ResizeObserver === "undefined") {
      setWidth(Math.floor(element.clientWidth));
      return;
    }
    const observer = new ResizeObserver(() => setWidth(Math.floor(element.clientWidth)));
    observer.observe(element);
    setWidth(Math.floor(element.clientWidth));
    return () => observer.disconnect();
  }, []);

  useLayoutEffect(() => {
    const target = canvas.current;
    if (!target || !width || !total) return;
    const height = 42;
    const scale = window.devicePixelRatio || 1;
    target.width = Math.floor(width * scale);
    target.height = Math.floor(height * scale);
    target.style.width = `${width}px`;
    target.style.height = `${height}px`;
    const context = target.getContext("2d");
    if (!context) return;
    context.setTransform(scale, 0, 0, scale, 0, 0);
    context.clearRect(0, 0, width, height);
    let left = 0;
    segments.forEach((segment, index) => {
      const right = index === segments.length - 1 ? width : left + width * Math.max(0, segment.value) / total;
      const seed = PALETTE[segment.color];
      for (let y = 0; y < height; y += CELL) {
        for (let x = Math.floor(left / CELL) * CELL; x < right; x += CELL) {
          if (x < left) continue;
          const threshold = BAYER[(y / CELL) % 4][(x / CELL) % 4] / 16;
          const alpha = threshold < 0.72 ? 0.92 : 0.38;
          context.fillStyle = `rgba(${seed.fill.join(",")},${alpha})`;
          context.fillRect(x, y, Math.min(CELL, right - x), CELL);
        }
      }
      context.strokeStyle = `rgb(${seed.line.join(",")})`;
      context.globalAlpha = 0.88;
      context.beginPath();
      context.moveTo(left + 0.5, 0);
      context.lineTo(left + 0.5, height);
      context.stroke();
      context.globalAlpha = 1;
      left = right;
    });
  }, [segments, total, width]);

  return <div className="dither-stacked" ref={host}>
    <canvas ref={canvas} role="img" aria-label={label} />
    <div className="dither-stacked-legend" aria-hidden="true">{segments.map(segment => {
      const percent = total ? segment.value / total * 100 : 0;
      return <span key={segment.key}><i data-color={segment.color} />{segment.label}<strong>{percent.toFixed(percent >= 10 ? 0 : 1)}%</strong></span>;
    })}</div>
  </div>;
}
