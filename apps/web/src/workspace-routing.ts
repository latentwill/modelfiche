export type ParsedWorkspaceRoute = {
  workspaceId: string;
  route: string;
  legacy: boolean;
};

export function parseWorkspaceHash(hash: string): ParsedWorkspaceRoute {
  const raw = hash.replace(/^#\/?/, "") || "dashboard";
  const [rawPath, rawQuery = ""] = raw.split("?", 2);
  const parts = rawPath.split("/").filter(Boolean);
  if (parts[0] === "w" && parts[1]) {
    const workspaceId = decodeURIComponent(parts[1]);
    const path = parts.slice(2).join("/") || "dashboard";
    return { workspaceId, route: `${path}${rawQuery ? `?${rawQuery}` : ""}`, legacy: false };
  }
  return { workspaceId: "", route: raw, legacy: true };
}

export function routeWorkspaceId(): string {
  if (typeof window === "undefined") return "";
  return parseWorkspaceHash(window.location.hash).workspaceId;
}

export function canonicalWorkspaceHref(href: string, workspaceId = routeWorkspaceId()): string {
  if (!workspaceId || !href.startsWith("#/")) return href;
  const parsed = parseWorkspaceHash(href);
  if (!parsed.legacy) return href;
  const [path, rawQuery = ""] = parsed.route.split("?", 2);
  const params = new URLSearchParams(rawQuery);
  params.delete("workspace");
  const query = params.toString();
  return `#/w/${encodeURIComponent(workspaceId)}/${path}${query ? `?${query}` : ""}`;
}

export function workspaceHref(path: string, params: Record<string, unknown> = {}, workspaceId = routeWorkspaceId()): string {
  const cleanPath = path.replace(/^#?\/?/, "");
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
  });
  const suffix = query.size ? `?${query.toString()}` : "";
  return workspaceId
    ? `#/w/${encodeURIComponent(workspaceId)}/${cleanPath}${suffix}`
    : `#/${cleanPath}${suffix}`;
}

export function legacyEntityReference(route: string): { entityType: string; entityId: string } | null {
  const [rawPath, rawQuery = ""] = route.split("?", 2);
  const [section = "", id = ""] = rawPath.split("/").filter(Boolean);
  const params = new URLSearchParams(rawQuery);
  const projectId = params.get("project");
  const entityTypes: Record<string, string> = {
    project: "project",
    dataset: "dataset",
    run: "run",
    samples: "run",
    training: "training-launch",
    checkpoint: "checkpoint",
    model: "model",
    "model-version": "model-version",
    eval: "eval-run",
    "eval-comparison": "eval-run",
    grid: "grid",
    jobs: "job",
    image: "asset",
  };
  if (id && entityTypes[section]) return { entityType: entityTypes[section], entityId: id };
  return projectId ? { entityType: "project", entityId: projectId } : null;
}
