export type ParsedWorkspaceRoute = {
  workspaceSlug: string;
  route: string;
  legacy: boolean;
};

export function parseWorkspaceHash(hash: string): ParsedWorkspaceRoute {
  const raw = hash.replace(/^#\/?/, "") || "dashboard";
  const [rawPath, rawQuery = ""] = raw.split("?", 2);
  const parts = rawPath.split("/").filter(Boolean);
  if (parts[0] === "w" && parts[1]) {
    // A malformed copied URL should remain navigable instead of crashing the app.
    let workspaceSlug = parts[1];
    try { workspaceSlug = decodeURIComponent(parts[1]); } catch { /* The API can report an unknown workspace. */ }
    const path = parts.slice(2).join("/") || "dashboard";
    return { workspaceSlug, route: `${path}${rawQuery ? `?${rawQuery}` : ""}`, legacy: false };
  }
  return { workspaceSlug: "", route: raw, legacy: true };
}

export function routeWorkspaceSlug(): string {
  if (typeof window === "undefined") return "";
  return parseWorkspaceHash(window.location.hash).workspaceSlug;
}

export function canonicalWorkspaceHref(href: string, workspaceSlug = routeWorkspaceSlug()): string {
  if (!workspaceSlug || !href.startsWith("#/")) return href;
  const parsed = parseWorkspaceHash(href);
  if (!parsed.legacy) return href;
  const [path, rawQuery = ""] = parsed.route.split("?", 2);
  const params = new URLSearchParams(rawQuery);
  params.delete("workspace");
  const query = params.toString();
  return `#/w/${encodeURIComponent(workspaceSlug)}/${path}${query ? `?${query}` : ""}`;
}

export function workspaceHref(path: string, params: Record<string, unknown> = {}, workspaceSlug = routeWorkspaceSlug()): string {
  const cleanPath = path.replace(/^#?\/?/, "");
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
  });
  const suffix = query.size ? `?${query.toString()}` : "";
  return workspaceSlug
    ? `#/w/${encodeURIComponent(workspaceSlug)}/${cleanPath}${suffix}`
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
