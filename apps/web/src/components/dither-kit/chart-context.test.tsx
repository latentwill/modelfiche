import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useChartController } from "./chart-context";

const controllerOptions = {
  chartType: "area" as const,
  config: { value: { label: "Loss", color: "blue" as const } },
  stackType: "default" as const,
  dimensions: { width: 320, height: 180 },
  margins: { top: 12, right: 12, bottom: 24, left: 36 },
};

describe("useChartController", () => {
  it("keeps series lifecycle callbacks stable across registration and live-data updates", () => {
    const { result, rerender } = renderHook(
      ({ data }: { data: Array<{ step: number; value: number }> }) => useChartController({ ...controllerOptions, data }),
      { initialProps: { data: [{ step: 1, value: 0.5 }] } },
    );
    const registerSeries = result.current.registerSeries;
    const unregisterSeries = result.current.unregisterSeries;

    act(() => result.current.registerSeries({ dataKey: "value", kind: "area", variant: "gradient", strokeVariant: "solid" }));
    rerender({ data: [{ step: 1, value: 0.5 }, { step: 2, value: 0.4 }] });

    expect(result.current.registerSeries).toBe(registerSeries);
    expect(result.current.unregisterSeries).toBe(unregisterSeries);
    expect(result.current.seriesSpecs).toHaveProperty("value");
  });
});
