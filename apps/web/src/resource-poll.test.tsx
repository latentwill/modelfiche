import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { pollWhileActive, useResource } from "./hooks";

function response(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

function Harness() {
  const resource = useResource<Record<string, unknown>>("/api/generation-queue/grid-1/grid-results", { pollInterval: 100, pollWhile: pollWhileActive });
  const progress = resource.data?.progress as Record<string, unknown> | undefined;
  return <div><span data-testid="status">{String(resource.data?.status ?? "")}</span><span data-testid="progress">{String(progress?.completed ?? "")}</span><span data-testid="loading">{String(resource.loading)}</span><button onClick={() => void resource.reload()}>Reload</button></div>;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("localStorage", { getItem: () => null });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useResource progress polling", () => {
  it.each(["completed", "succeeded", "failed", "canceled", "cancelled"])("recognizes %s as terminal", status => {
    expect(pollWhileActive({ status })).toBe(false);
  });

  it("keeps polling only while at least one collection item is active", () => {
    expect(pollWhileActive({ items: [{ status: "completed" }, { status: "failed" }] })).toBe(false);
    expect(pollWhileActive({ items: [{ status: "completed" }, { status: "running" }] })).toBe(true);
  });

  it("refreshes running queue results and stops after a terminal state", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ status: "running", progress: { completed: 0, total: 4 } }))
      .mockResolvedValueOnce(response({ status: "running", progress: { completed: 2, total: 4 } }))
      .mockResolvedValueOnce(response({ status: "completed", progress: { completed: 4, total: 4 } }));
    vi.stubGlobal("fetch", fetchMock);

    render(<Harness />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByTestId("progress")).toHaveTextContent("0");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(screen.getByTestId("progress")).toHaveTextContent("2");
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(screen.getByTestId("status")).toHaveTextContent("completed");
    expect(screen.getByTestId("progress")).toHaveTextContent("4");
    expect(fetchMock).toHaveBeenCalledTimes(3);

    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
  it("preserves rendered data during a manual refresh", async () => {
    let resolveRefresh!: (value: Response) => void;
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ status: "running", progress: { completed: 1, total: 2 } }))
      .mockImplementationOnce(() => new Promise<Response>(resolve => { resolveRefresh = resolve; }));
    vi.stubGlobal("fetch", fetchMock);

    render(<Harness />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByTestId("progress")).toHaveTextContent("1");

    fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    await act(async () => {});
    expect(screen.getByTestId("progress")).toHaveTextContent("1");
    expect(screen.getByTestId("loading")).toHaveTextContent("false");

    await act(async () => { resolveRefresh(response({ status: "completed", progress: { completed: 2, total: 2 } })); });
    expect(screen.getByTestId("progress")).toHaveTextContent("2");
  });

  it("restarts polling when a manual refresh discovers new work", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ status: "completed", progress: { completed: 1, total: 1 } }))
      .mockResolvedValueOnce(response({ status: "completed", progress: { completed: 1, total: 1 } }))
      .mockResolvedValueOnce(response({ status: "running", progress: { completed: 0, total: 1 } }))
      .mockResolvedValueOnce(response({ status: "completed", progress: { completed: 1, total: 1 } }));
    vi.stubGlobal("fetch", fetchMock);

    render(<Harness />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    await act(async () => {});
    expect(screen.getByTestId("status")).toHaveTextContent("running");
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(screen.getByTestId("status")).toHaveTextContent("completed");
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

});
