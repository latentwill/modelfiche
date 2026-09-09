import { routeWorkspaceSlug } from "./workspace-routing";

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
export const WORKSPACE_STORAGE_KEY = "titles.activeWorkspaceId";

export function activeWorkspaceId(): string {
  try {
    return localStorage.getItem(WORKSPACE_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setActiveWorkspaceId(workspaceId: string) {
  localStorage.setItem(WORKSPACE_STORAGE_KEY, workspaceId);
  localStorage.removeItem("titles.activeProfileId");
}
export async function establishRemoteSession(clientId: string, clientSecret: string): Promise<void> {
  await api("/api/remote-session", {
    method: "POST",
    headers: {
      "cf-access-client-id": clientId.trim(),
      "cf-access-client-secret": clientSecret,
    },
  });
}




export async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) headers.set("content-type", "application/json");
  headers.set("accept", "application/json");
  const profileId = localStorage.getItem("titles.activeProfileId");
  if (profileId) headers.set("x-profile-id", profileId);
  const workspaceSlug = routeWorkspaceSlug();
  if (workspaceSlug) headers.set("x-workspace-id", workspaceSlug);
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  const contentType = response.headers.get("content-type") ?? "";
  const text = await response.text();
  const body = text ? (contentType.includes("json") ? JSON.parse(text) : text) : undefined;
  if (!response.ok) {
    const detail = typeof body === "object" && body && "detail" in body ? body.detail : undefined;
    const message = detail !== undefined ? String(detail) : `${response.status} ${response.statusText}`;
    throw new ApiError(message, response.status, body);
  }
  return body as T;
}

export async function apiUnscoped<T = unknown>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { accept: "application/json" },
  });
  const contentType = response.headers.get("content-type") ?? "";
  const text = await response.text();
  const body = text ? (contentType.includes("json") ? JSON.parse(text) : text) : undefined;
  if (!response.ok) {
    const detail = typeof body === "object" && body && "detail" in body ? body.detail : undefined;
    throw new ApiError(detail !== undefined ? String(detail) : `${response.status} ${response.statusText}`, response.status, body);
  }
  return body as T;
}

export async function apiDownload(path: string): Promise<{ blob: Blob; filename: string }> {
  const headers = new Headers({ accept: "application/octet-stream" });
  const profileId = localStorage.getItem("titles.activeProfileId");
  if (profileId) headers.set("x-profile-id", profileId);
  const workspaceSlug = routeWorkspaceSlug();
  if (workspaceSlug) headers.set("x-workspace-id", workspaceSlug);
  const response = await fetch(`${API_BASE}${path}`, { headers });
  if (!response.ok) {
    const text = await response.text();
    let body: unknown = text;
    try { body = text ? JSON.parse(text) : undefined; } catch { /* Preserve the response text. */ }
    const detail = typeof body === "object" && body && "detail" in body ? body.detail : undefined;
    const message = detail !== undefined ? String(detail) : `${response.status} ${response.statusText}`;
    throw new ApiError(message, response.status, body);
  }
  const disposition = response.headers.get("content-disposition") ?? "";
  const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] ?? "modelfiche-support.zip";
  return { blob: await response.blob(), filename };
}

export function listOf<T>(value: unknown): T[] {
  if (Array.isArray(value)) return value as T[];
  if (!value || typeof value !== "object") return [];
  for (const key of ["items", "results", "data", "profiles", "projects", "datasets", "runs", "assets", "models", "eval_runs", "grids", "reviews", "comments", "sources", "objects", "samples", "checkpoints"]) {
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
export function routeQuery(params: Record<string, unknown>, _workspaceId?: string) {
  return query(params);
}
