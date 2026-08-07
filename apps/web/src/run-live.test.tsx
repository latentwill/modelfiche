import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRunLive } from "./hooks";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readonly listeners: Record<string, EventListener[]> = {};
  readonly url: string;
  closed = false;
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(url: string | URL) {
    this.url = String(url);
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListener) {
    (this.listeners[type] ??= []).push(listener);
  }

  close() {
    this.closed = true;
  }

  emit(type: "run.snapshot" | "run.update", data: Record<string, unknown>) {
    const event = new MessageEvent(type, { data: JSON.stringify(data) });
    this.listeners[type]?.forEach(listener => listener(event));
  }
}

function response(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

function Harness({ id = "run-1" }: { id?: string }) {
  const live = useRunLive(id);
  return <div>
    <span data-testid="connection">{live.connection}</span>
    <span data-testid="step">{String(live.data?.current_step ?? "")}</span>
    <span data-testid="status">{String(live.data?.status ?? "")}</span>
    <span data-testid="error">{live.error}</span>
  </div>;
}

async function flushSnapshot() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("localStorage", { getItem: () => null });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useRunLive", () => {
  it("fetches an immediate snapshot, applies named SSE updates, and reconnects with backoff", async () => {
    const fetchMock = vi.fn(async () => response({ run_id: "run-1", status: "running", current_step: 7 }));
    vi.stubGlobal("fetch", fetchMock);

    render(<Harness />);
    await flushSnapshot();

    expect(fetchMock).toHaveBeenCalledWith("/api/runs/run-1/live", expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(screen.getByTestId("step")).toHaveTextContent("7");
    expect(FakeEventSource.instances).toHaveLength(1);
    const first = FakeEventSource.instances[0];
    expect(first.url).toBe("/api/runs/run-1/events");

    act(() => first.onopen?.(new Event("open")));
    expect(screen.getByTestId("connection")).toHaveTextContent("connected");
    act(() => first.emit("run.snapshot", { status: "running", current_step: 8 }));
    act(() => first.emit("run.update", { current_step: 9, latest_loss: { name: "train/objective", value: 0.2 } }));
    expect(screen.getByTestId("step")).toHaveTextContent("9");

    act(() => first.onerror?.(new Event("error")));
    expect(first.closed).toBe(true);
    expect(screen.getByTestId("connection")).toHaveTextContent("reconnecting");
    expect(screen.getByTestId("error")).toHaveTextContent("Live updates interrupted");
    expect(FakeEventSource.instances).toHaveLength(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(999); });
    expect(FakeEventSource.instances).toHaveLength(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(FakeEventSource.instances).toHaveLength(2);
    act(() => FakeEventSource.instances[1].onopen?.(new Event("open")));
    expect(screen.getByTestId("connection")).toHaveTextContent("connected");
    expect(screen.getByTestId("error")).toBeEmptyDOMElement();
  });

  it("stops on a terminal update and closes cleanly on teardown", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({ run_id: "run-1", status: "running", current_step: 12 })));
    const view = render(<Harness />);
    await flushSnapshot();
    const source = FakeEventSource.instances[0];

    act(() => source.emit("run.update", { status: "completed", current_step: 13 }));
    expect(source.closed).toBe(true);
    expect(screen.getByTestId("status")).toHaveTextContent("completed");
    expect(screen.getByTestId("connection")).toHaveTextContent("stopped");
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    expect(FakeEventSource.instances).toHaveLength(1);

    view.unmount();
    expect(source.closed).toBe(true);
  });

  it.each(["succeeded", "failed", "interrupted", "canceled", "cancelled"])("does not open an event stream for an already-terminal %s snapshot", async status => {
    vi.stubGlobal("fetch", vi.fn(async () => response({ run_id: "run-1", status, current_step: 3 })));

    render(<Harness />);
    await flushSnapshot();

    expect(screen.getByTestId("status")).toHaveTextContent(status);
    expect(screen.getByTestId("connection")).toHaveTextContent("stopped");
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it("cancels a pending reconnect when the component unmounts", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({ run_id: "run-1", status: "running" })));
    const view = render(<Harness />);
    await flushSnapshot();
    const source = FakeEventSource.instances[0];
    act(() => source.onerror?.(new Event("error")));

    view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });

    expect(source.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(1);
  });
});
