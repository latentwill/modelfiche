export type Entity = Record<string, unknown> & { id: string };

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly details?: unknown,
  ) {
    super(message);
  }
}

const configuredBase = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "");
export const API_BASE = configuredBase ?? "";

export async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) headers.set("content-type", "application/json");
  headers.set("accept", "application/json");
  const profileId = localStorage.getItem("titles.activeProfileId");
  if (profileId) headers.set("x-profile-id", profileId);
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  const contentType = response.headers.get("content-type") ?? "";
  const text = await response.text();
  const body = text ? (contentType.includes("json") ? JSON.parse(text) : text) : undefined;
  if (!response.ok) {
    const message = typeof body === "object" && body && "detail" in body
      ? String((body as { detail: unknown }).detail)
      : `${response.status} ${response.statusText}`;
    throw new ApiError(message, response.status, body);
  }
  return body as T;
}

export function listOf<T>(value: unknown): T[] {
  if (Array.isArray(value)) return value as T[];
  if (!value || typeof value !== "object") return [];
  for (const key of ["items", "results", "data", "profiles", "projects", "datasets", "runs", "assets", "models", "prompt_sets", "eval_runs", "grids", "reviews", "comments", "sources", "objects", "samples", "checkpoints"]) {
    const candidate = (value as Record<string, unknown>)[key];
    if (Array.isArray(candidate)) return candidate as T[];
  }
  return [];
}

export function jsonBody(value: unknown): RequestInit {
  return { method: "POST", body: JSON.stringify(value) };
}

export function patchBody(value: unknown): RequestInit {
  return { method: "PATCH", body: JSON.stringify(value) };
}


export function str(value: unknown, fallback = "-") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

export function idOf(value: Record<string, unknown>) {
  return str(value.id ?? value.asset_revision_id ?? value.asset_id ?? value.uuid, "");
}

export function query(params: Record<string, unknown>) {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") q.set(key, String(value));
  });
  const encoded = q.toString();
  return encoded ? `?${encoded}` : "";
}
