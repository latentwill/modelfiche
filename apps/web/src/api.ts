import { routeWorkspaceSlug } from "./workspace-routing";
import { readPreference, removePreference, writePreference } from "./preferences";

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
  return readPreference(WORKSPACE_STORAGE_KEY) ?? "";
}

export function setActiveWorkspaceId(workspaceId: string) {
  if (workspaceId === activeWorkspaceId()) return;
  writePreference(WORKSPACE_STORAGE_KEY, workspaceId);
  removePreference("titles.activeProfileId");
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




function detailMessage(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(detailMessage).filter(Boolean).join("; ");
  if (detail && typeof detail === "object") {
    const value = detail as Record<string, unknown>;
    const message = detailMessage(value.message ?? value.msg ?? value.detail);
    const location = Array.isArray(value.loc) ? value.loc.filter(part => !["body", "query", "path"].includes(String(part))).join(" · ") : "";
    return message && location ? `${location}: ${message}` : message;
  }
  return "";
}

async function responseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return undefined;
  if (!(response.headers.get("content-type") ?? "").includes("json")) return text;
  try { return JSON.parse(text); } catch {
    if (response.ok) throw new ApiError("The server returned an unreadable response. Try again.", response.status);
    return undefined;
  }
}

function responseError(response: Response, body: unknown): ApiError {
  const detail = body && typeof body === "object" && "detail" in body ? body.detail : body;
  // HTML proxy responses may contain implementation details; use the status instead.
  const message = typeof detail === "string" && /<[^>]+>/.test(detail) ? "" : detailMessage(detail);
  return new ApiError(message || `${response.status} ${response.statusText || "Request failed"}. Try again.`, response.status, body);
}

export async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) headers.set("content-type", "application/json");
  headers.set("accept", "application/json");
  const profileId = readPreference("titles.activeProfileId");
  if (profileId) headers.set("x-profile-id", profileId);
  const workspaceSlug = routeWorkspaceSlug();
  if (workspaceSlug) headers.set("x-workspace-id", workspaceSlug);
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  const body = await responseBody(response);
  if (!response.ok) throw responseError(response, body);
  return body as T;
}

export async function apiUnscoped<T = unknown>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { accept: "application/json" },
  });
  const body = await responseBody(response);
  if (!response.ok) throw responseError(response, body);
  return body as T;
}

export async function apiDownload(path: string): Promise<{ blob: Blob; filename: string }> {
  const headers = new Headers({ accept: "application/octet-stream" });
  const profileId = readPreference("titles.activeProfileId");
  if (profileId) headers.set("x-profile-id", profileId);
  const workspaceSlug = routeWorkspaceSlug();
  if (workspaceSlug) headers.set("x-workspace-id", workspaceSlug);
  const response = await fetch(`${API_BASE}${path}`, { headers });
  if (!response.ok) {
    throw responseError(response, await responseBody(response));
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
