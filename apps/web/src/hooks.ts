import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { API_BASE, api } from "./api";
import { canonicalWorkspaceHref, parseWorkspaceHash, routeWorkspaceSlug } from "./workspace-routing";

type ResourceOptions<T> = {
  pollInterval?: number;
  pollWhile?: (data: T | null) => boolean;
};

export function useResource<T = unknown>(path: string | null, options: ResourceOptions<T> = {}) {
  const workspaceSlug = routeWorkspaceSlug();
  const requestKey = path === null ? null : `${workspaceSlug}\n${path}`;
  const [data, setData] = useState<T | null>(null);
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const requestId = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);
  const [pollGeneration, setPollGeneration] = useState(0);
  const latestData = useRef<T | null>(null);
  latestData.current = loadedKey === requestKey ? data : null;
  const load = useCallback(async (preserveData = false): Promise<T | null> => {
    const id = ++requestId.current;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    if (!path) {
      controller.abort();
      setData(null);
      setError("");
      setErrorKey(null);
      setLoadedKey(null);
      setLoading(false);
      return null;
    }
    if (!preserveData) setData(null);
    setLoading(true);
    setErrorKey(null);
    setError("");
    try {
      const result = await api<T>(path, { signal: controller.signal });
      if (id === requestId.current) {
        setData(result);
        setLoadedKey(requestKey);
      }
      return result;
    } catch (reason) {
      if (id === requestId.current && !(reason instanceof DOMException && reason.name === "AbortError")) {
        setError(reason instanceof Error ? reason.message : String(reason));
        setErrorKey(requestKey);
      }
      return null;
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }, [path, requestKey]);
  useEffect(() => () => {
    controllerRef.current?.abort();
  }, [path, requestKey]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!path || !options.pollInterval || options.pollInterval <= 0) return;
    let active = true;
    let timer: number | undefined;
    const poll = () => {
      if (!active || (options.pollWhile && !options.pollWhile(latestData.current))) return;
      timer = window.setTimeout(async () => {
        const result = await load(true);
        if (active && (!options.pollWhile || options.pollWhile(result))) poll();
      }, options.pollInterval);
    };
    poll();
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [load, options.pollInterval, options.pollWhile, path, pollGeneration]);
  const currentError = errorKey === requestKey ? error : "";
  const currentData = loadedKey === requestKey ? data : null;
  const currentLoading = !path ? false : loadedKey !== requestKey && !currentError ? true : loading && currentData === null;
  return { data: currentData, error: currentError, loading: currentLoading, reload: async () => { await load(true); setPollGeneration(value => value + 1); }, setData };
}

const TERMINAL_PROGRESS_STATUSES: Record<string, true> = { completed: true, succeeded: true, failed: true, canceled: true, cancelled: true };
export function pollWhileActive(data: unknown | null) {
  const record = data && typeof data === "object" && !Array.isArray(data) ? data as Record<string, unknown> : null;
  const entries = Array.isArray(data) ? data : record?.items;
  if (Array.isArray(entries)) return entries.length === 0 || entries.some(item => {
    const status = item && typeof item === "object" ? String((item as Record<string, unknown>).status ?? "").toLowerCase() : "";
    return !TERMINAL_PROGRESS_STATUSES[status];
  });
  const status = String(record?.status ?? "").toLowerCase();
  return !TERMINAL_PROGRESS_STATUSES[status];
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
  succeeded: true,
  failed: true,
  interrupted: true,
  canceled: true,
  cancelled: true,
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

function workspaceRouteHref(href: string) {
  return canonicalWorkspaceHref(href);
}

export function useCanonicalWorkspaceLinks(workspaceId: string) {
  useLayoutEffect(() => {
    if (!workspaceId) return;
    const rewrite = (root: ParentNode) => {
      const links = root instanceof HTMLAnchorElement
        ? [root]
        : Array.from(root.querySelectorAll<HTMLAnchorElement>("a[href^='#/']"));
      for (const link of links) {
        const href = link.getAttribute("href");
        if (!href) continue;
        const canonical = canonicalWorkspaceHref(href, workspaceId);
        if (canonical !== href) link.setAttribute("href", canonical);
      }
    };
    const observer = new MutationObserver(records => {
      for (const record of records) {
        if (record.type === "attributes" && record.target instanceof HTMLAnchorElement) {
          rewrite(record.target);
          continue;
        }
        for (const node of record.addedNodes) {
          if (node instanceof Element) rewrite(node);
        }
      }
    });
    rewrite(document);
    observer.observe(document.body, { attributes: true, attributeFilter: ["href"], childList: true, subtree: true });
    return () => observer.disconnect();
  }, [workspaceId]);
}

export function useHashRoute() {
  const readHash = () => window.location.hash;
  const [hash, setHash] = useState(readHash);
  const modeRef = useRef<"initial" | "push" | "pop">("initial");
  const pendingRestore = useRef<{ scrollY: number; focusId: string } | null>(null);
  useEffect(() => {
    const current = history.state?.modelficheRoute;
    if (!current) history.replaceState({ ...(history.state ?? {}), modelficheRoute: { scrollY: window.scrollY, focusId: document.activeElement instanceof HTMLElement ? document.activeElement.id : "" } }, "", window.location.href);
    const onClick = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const target = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>("a[href^='#/']") : null;
      if (!target) return;
      event.preventDefault();
      history.replaceState({ ...(history.state ?? {}), modelficheRoute: { scrollY: window.scrollY, focusId: document.activeElement instanceof HTMLElement ? document.activeElement.id : "" } }, "", window.location.href);
      const href = target.getAttribute("href") ?? target.hash;
      history.pushState({ modelficheRoute: { scrollY: 0, focusId: "" } }, "", workspaceRouteHref(href));
      modeRef.current = "push";
      setHash(readHash());
    };
    const onPopState = (event: PopStateEvent) => {
      modeRef.current = "pop";
      pendingRestore.current = event.state?.modelficheRoute ?? { scrollY: 0, focusId: "" };
      setHash(readHash());
    };
    const onHashChange = () => {
      if (modeRef.current === "initial") modeRef.current = "push";
      setHash(readHash());
    };
    document.addEventListener("click", onClick);
    window.addEventListener("popstate", onPopState);
    window.addEventListener("hashchange", onHashChange);
    return () => { document.removeEventListener("click", onClick); window.removeEventListener("popstate", onPopState); window.removeEventListener("hashchange", onHashChange); };
  }, []);
  useEffect(() => {
    if (modeRef.current === "initial") return;
    const mode = modeRef.current;
    const restore = pendingRestore.current;
    requestAnimationFrame(() => {
      if (mode === "pop" && restore) {
        window.scrollTo({ top: restore.scrollY, behavior: "auto" });
        if (restore.focusId) document.getElementById(restore.focusId)?.focus();
      } else {
        window.scrollTo({ top: 0, behavior: "auto" });
        const heading = document.querySelector<HTMLElement>("#vela-route-heading, .vela-route-stage h1, main h1");
        if (heading) { if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1"); heading.focus({ preventScroll: true }); }
      }
    });
    modeRef.current = "initial";
    pendingRestore.current = null;
  }, [hash]);
  return parseWorkspaceHash(hash).route;
}
