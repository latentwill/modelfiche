import { useState, type MouseEvent } from "react";
import { AreaChart } from "./components/dither-kit/area-chart";
import { Area } from "./components/dither-kit/area";
import { Grid } from "./components/dither-kit/grid";
import { XAxis } from "./components/dither-kit/x-axis";
import { YAxis } from "./components/dither-kit/y-axis";

export type DitherMetricPoint = { step: number; value: number };
const LOSS_CHART_CONFIG = { value: { label: "Loss", color: "blue" } } as const;
const LOSS_CHART_MARGINS = { top: 18, right: 18, bottom: 34, left: 54 };

export function DitherLossChart({ points }: { points: DitherMetricPoint[] }) {
  const [hover, setHover] = useState<{ point: DitherMetricPoint; left: number; top: number } | null>(null);
  const axisStep = (points.at(-1)?.step ?? 0) <= 2000 ? 250 : 500;
  const inspect = (event: MouseEvent<HTMLDivElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const plotLeft = 70;
    const plotWidth = Math.max(1, bounds.width - plotLeft - 34);
    const fraction = Math.max(0, Math.min(1, (event.clientX - bounds.left - plotLeft) / plotWidth));
    const index = Math.round(fraction * (points.length - 1));
    setHover({ point: points[index], left: event.clientX - bounds.left, top: event.clientY - bounds.top });
  };
  return <div className="dither-loss-chart" role="img" aria-label="Loss over training step" onMouseMove={inspect} onMouseLeave={() => setHover(null)}>
    <div className="dither-chart-kicker"><span>Training loss</span><strong>{points.at(-1)?.value.toFixed(4)}</strong></div>
    <AreaChart className="dither-chart-root" data={points} config={LOSS_CHART_CONFIG} margins={LOSS_CHART_MARGINS} bloom="aura" animationDuration={1100}>
      <Grid horizontal vertical={false} strokeDasharray="2 5" />
      <XAxis dataKey="step" maxTicks={9} tickStep={axisStep} tickFormatter={value => String(value)} />
      <YAxis tickCount={5} tickFormatter={value => value.toFixed(value < 1 ? 2 : 1)} />
      <Area dataKey="value" variant="gradient" />
    </AreaChart>
    {hover && <div className="dither-tooltip dither-tooltip-manual" style={{ left: hover.left, top: hover.top }}>
      <div>Step {hover.point.step}</div>
      <div><div><span /><span>Loss</span><span>{hover.point.value.toFixed(5)}</span></div></div>
    </div>}
  </div>;
}
