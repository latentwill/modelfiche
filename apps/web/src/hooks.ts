import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

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
