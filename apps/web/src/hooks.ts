import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE, api } from "./api";

export function useResource<T = unknown>(path: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [loadedPath, setLoadedPath] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const requestId = useRef(0);
  const load = useCallback(async () => {
    const id = ++requestId.current;
    if (!path) {
      setData(null);
      setError("");
      setLoadedPath(null);
      setLoading(false);
      return;
    }
    setData(null);
    setLoading(true);
    setError("");
    try {
      const result = await api<T>(path);
      if (id === requestId.current) {
        setData(result);
        setLoadedPath(path);
      }
    } catch (reason) {
      if (id === requestId.current) setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }, [path]);
  useEffect(() => { void load(); }, [load]);
  return { data: loadedPath === path ? data : null, error, loading, reload: load, setData };
}

export type RunLiveProjection = Record<string, unknown> & {
  run_id?: string;
  status?: string;
  current_step?: number | null;
  latest_loss?: Record<string, unknown> | null;
  learning_rate?: Record<string, unknown> | number | string | null;
  elapsed_seconds?: number | null;
  started_at?: string | null;
  finished_at?: string | null;
  last_event_at?: string | null;
  summaries?: unknown;
  uploads?: unknown;
  sample_count?: number | null;
};

export type LiveConnectionState = "connecting" | "connected" | "reconnecting" | "stopped";

const TERMINAL_RUN_STATUSES: Record<string, true> = {
  completed: true,
  failed: true,
  interrupted: true,
};

export function useRunLive(runId: string) {
  const [data, setData] = useState<RunLiveProjection | null>(null);
  const [connection, setConnection] = useState<LiveConnectionState>("connecting");
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let source: EventSource | null = null;
    let reconnectTimer: number | undefined;
    let reconnectAttempt = 0;

    const terminal = (projection: RunLiveProjection) => Boolean(TERMINAL_RUN_STATUSES[String(projection.status ?? "").toLowerCase()]);
    const closeSource = () => {
      source?.close();
      source = null;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      reconnectTimer = undefined;
    };
    const stop = () => {
      closeSource();
      if (active) setConnection("stopped");
    };
    const applyProjection = (projection: RunLiveProjection) => {
      if (!active) return;
      setData(current => ({ ...(current ?? {}), ...projection }));
      setRevision(current => current + 1);
      if (terminal(projection)) stop();
    };
    const readEvent = (event: MessageEvent<string>) => {
      try {
        const parsed = JSON.parse(event.data) as RunLiveProjection;
        applyProjection(parsed);
      } catch {
        if (active) setError("A live training update could not be read.");
      }
    };
    const connect = () => {
      if (!active || source) return;
      setConnection(reconnectAttempt ? "reconnecting" : "connecting");
      source = new EventSource(`${API_BASE}/api/runs/${encodeURIComponent(runId)}/events`);
      source.addEventListener("run.snapshot", readEvent as EventListener);
      source.addEventListener("run.update", readEvent as EventListener);
      source.onopen = () => {
        if (!active) return;
        reconnectAttempt = 0;
        setError("");
        setConnection("connected");
      };
      source.onerror = () => {
        if (!active) return;
        source?.close();
        source = null;
        setError("Live updates interrupted; reconnecting.");
        setConnection("reconnecting");
        const delay = Math.min(1_000 * (2 ** reconnectAttempt), 10_000);
        reconnectAttempt += 1;
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };

    setData(null);
    setRevision(0);
    setError("");
    setConnection("connecting");
    void api<RunLiveProjection>(`/api/runs/${runId}/live`, { signal: controller.signal })
      .then(snapshot => {
        if (!active) return;
        applyProjection(snapshot);
        if (!terminal(snapshot)) connect();
      })
      .catch(reason => {
        if (!active || controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : String(reason));
        connect();
      });

    return () => {
      active = false;
      controller.abort();
      closeSource();
    };
  }, [runId]);

  return { data, connection, error, revision };
}

export function useHashRoute() {
  const read = () => window.location.hash.replace(/^#\/?/, "") || "projects";
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const update = () => setRoute(read());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return route;
}
