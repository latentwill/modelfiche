import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

export function useResource<T = unknown>(path: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const load = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    setError("");
    try {
      setData(await api<T>(path));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, [path]);
  useEffect(() => { void load(); }, [load]);
  return { data, error, loading, reload: load, setData };
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
