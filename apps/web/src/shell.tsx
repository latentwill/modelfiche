import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import {
  Activity,
  BookOpen,
  ArrowDownToLine,
  ArrowLeftRight,
  Bold,
  Box,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Database,
  Folder,
  Images,
  ImagePlus,
  Italic,
  LayoutDashboard,
  List,
  Menu,
  Moon,
  PanelLeftClose,
  PanelRightClose,
  PanelRightOpen,
  Plus,
  Search,
  Settings,
  Sun,
  X,
} from "lucide-react";
import { api, idOf, jsonBody, listOf, query, routeQuery, setActiveWorkspaceId, str } from "./api";
import { ActiveTrainingNavigation } from "./active-training";
import { activityDisplayText, activityLabel, activityObjectHref, previewAssetId } from "./activity";
import { galleryOpenHref } from "./gallery-routing";
import { AssetImage } from "./asset-image";
import { useCanonicalWorkspaceLinks, useResource } from "./hooks";
import { canonicalWorkspaceHref, parseWorkspaceHash, routeWorkspaceSlug, workspaceHref } from "./workspace-routing";
import { CopyMetadataButton, Dropdown, Notice, ProfileSelect, Status, useDragTextSelection, useFocusWorkspace } from "./ui";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);
const workspaceSlugOf = (workspace: Row) => str(workspace.slug, idOf(workspace));
export const SHELL_LAYOUT_CONTRACT = {
  navigation: "scroll-region",
  footer: "viewport-docked",
  inspector: "scroll-region",
  viewport: "100dvh",
} as const;
export type ThemeMode = "light" | "dark";
export const THEME_STORAGE_KEY = "modelfiche.theme";

function validTheme(value: string | null | undefined): ThemeMode | null {
  return value === "light" || value === "dark" ? value : null;
}

function themeStorage(): Storage | null {
  try {
    return typeof globalThis !== "undefined" && "localStorage" in globalThis ? globalThis.localStorage : null;
  } catch {
    return null;
  }
}

function initialTheme(): ThemeMode {
  let stored: ThemeMode | null = null;
  try {
    stored = validTheme(themeStorage()?.getItem(THEME_STORAGE_KEY));
  } catch {
    stored = null;
  }
  if (stored) return stored;
  const declared = typeof document !== "undefined" ? validTheme(document.documentElement.dataset.theme) : null;
  if (declared) return declared;
  try {
    return typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

function applyTheme(theme: ThemeMode) {
  if (typeof document !== "undefined") document.documentElement.dataset.theme = theme;
}

export function useThemeController() {
  const [theme, setTheme] = useState<ThemeMode>(() => {
    const next = initialTheme();
    applyTheme(next);
    return next;
  });
  useLayoutEffect(() => { applyTheme(theme); }, [theme]);
  useEffect(() => {
    try {
      themeStorage()?.setItem(THEME_STORAGE_KEY, theme);
    } catch {
      // Storage may be unavailable in privacy-restricted contexts.
    }
    if (typeof window === "undefined") return;
    const onStorage = (event: StorageEvent) => {
      if (event.key !== THEME_STORAGE_KEY) return;
      const next = validTheme(event.newValue);
      if (next) setTheme(next);
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [theme]);
  return {
    theme,
    toggleTheme: () => setTheme(current => current === "dark" ? "light" : "dark"),
  };
}


export function WorkbenchShell({ section, id, params, children, theme: controlledTheme, onToggleTheme: controlledToggleTheme }: { section: string; id: string; params: URLSearchParams; children: ReactNode; theme?: ThemeMode; onToggleTheme?: () => void }) {
  const localTheme = useThemeController();
  const theme = controlledTheme ?? localTheme.theme;
  const toggleTheme = controlledToggleTheme ?? localTheme.toggleTheme;
  const [leftCollapsed, setLeftCollapsed] = useStoredBoolean("titles.leftCollapsed");
  const [rightCollapsed, setRightCollapsed] = useStoredBoolean("titles.rightCollapsed");
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [mobileInspectorOpen, setMobileInspectorOpen] = useState(false);
  const navOpenerRef = useRef<HTMLButtonElement>(null);
  const inspectorOpenerRef = useRef<HTMLButtonElement>(null);
  const projects = useResource<unknown>("/api/projects?limit=100");
  const workspaces = useResource<unknown>("/api/workspaces");
  const availableWorkspaces = rows(workspaces.data);
  const selectedWorkspaceSlug = routeWorkspaceSlug();
  const workspace = availableWorkspaces.find(item => workspaceSlugOf(item) === selectedWorkspaceSlug) ?? null;
  const workspaceId = workspace ? idOf(workspace) : "";
  useCanonicalWorkspaceLinks(selectedWorkspaceSlug);
  const scopedProjects = workspaceId ? rows(projects.data).filter(project => !project.workspace_id || str(project.workspace_id) === workspaceId) : [];
  const projectId = projectForRoute(section, id, params);
  const closeNavigation = () => { setMobileNavOpen(false); navOpenerRef.current?.focus(); };
  const closeInspector = () => { setMobileInspectorOpen(false); inspectorOpenerRef.current?.focus(); };
  useEffect(() => {
    if (workspaceId) setActiveWorkspaceId(workspaceId);
  }, [workspaceId]);
  useEffect(() => { setMobileNavOpen(false); setMobileInspectorOpen(false); }, [section, id]);
  useEffect(() => {
    const update = (event: Event) => {
      const next = (event as CustomEvent<Row>).detail;
      if (!next?.id) return;
      workspaces.setData(rows(workspaces.data).map(item => idOf(item) === idOf(next) ? next : item));
    };
    window.addEventListener("titles:workspace-updated", update);
    return () => window.removeEventListener("titles:workspace-updated", update);
  }, [workspaces.data, workspaces.setData]);
  useEffect(() => {
    const update = (event: Event) => {
      if (!(event instanceof CustomEvent)) return;
      const next: unknown = event.detail;
      if (!next || typeof next !== "object" || !("id" in next)) return;
      projects.setData(rows(projects.data).map(item => idOf(item) === idOf(next) ? next : item));
    };
    window.addEventListener("titles:project-updated", update);
    return () => window.removeEventListener("titles:project-updated", update);
  }, [projects.data, projects.setData]);
  return <div className="vela-shell" data-nav-collapsed={leftCollapsed} data-inspector-collapsed={rightCollapsed}>
    <Sidebar section={section} id={id} activeProjectId={projectId} projects={scopedProjects} workspaceSlug={selectedWorkspaceSlug} theme={theme} collapsed={leftCollapsed} mobileOpen={mobileNavOpen} restoreFocusRef={navOpenerRef} onMobileClose={closeNavigation} onCollapse={() => setLeftCollapsed(!leftCollapsed)} />
    <div className="vela-workspace">
      <Topbar section={section} id={id} projectId={projectId} workspaces={availableWorkspaces} workspaceSlug={selectedWorkspaceSlug} theme={theme} onToggleTheme={toggleTheme} navOpenerRef={navOpenerRef} inspectorOpenerRef={inspectorOpenerRef} onOpenNavigation={() => { setLeftCollapsed(false); setMobileInspectorOpen(false); setMobileNavOpen(true); }} onOpenInspector={() => { setRightCollapsed(false); setMobileNavOpen(false); setMobileInspectorOpen(true); }} />
      <a className="vela-skip-link" href="#vela-route-heading">Skip to main content</a>
      <div className="vela-route-stage" id="vela-route-main" role="main" tabIndex={-1}>
        {isCreateRoute(section, params) && <div className="vela-creating-scope" role="status">Creating in: <strong>{str(workspace?.name, "Unknown workspace")}</strong></div>}
        {children}
      </div>
    </div>
    <ContextInspector section={section} id={id} projectId={projectId} collapsed={rightCollapsed} mobileOpen={mobileInspectorOpen} restoreFocusRef={inspectorOpenerRef} onMobileClose={closeInspector} onCollapse={() => setRightCollapsed(!rightCollapsed)} />
    {(mobileNavOpen || mobileInspectorOpen) && <button className="vela-drawer-scrim" aria-label="Close open panel" onClick={() => { closeNavigation(); closeInspector(); }} />}
  </div>;
}

function isCreateRoute(section: string, params: URLSearchParams): boolean {
  return (
    section === "training-new"
    || section === "generate"
    || section === "import"
    || (section === "transfers" && params.get("mode") === "import")
    || (section === "dashboard" && params.get("create") === "project")
  );
}
function Topbar({ section, id, projectId, workspaces, workspaceSlug, theme, onToggleTheme, navOpenerRef, inspectorOpenerRef, onOpenNavigation, onOpenInspector }: { section: string; id: string; projectId: string; workspaces: Row[]; workspaceSlug: string; theme: ThemeMode; onToggleTheme: () => void; navOpenerRef: RefObject<HTMLButtonElement | null>; inspectorOpenerRef: RefObject<HTMLButtonElement | null>; onOpenNavigation: () => void; onOpenInspector: () => void }) {
  const [createOpen, setCreateOpen] = useState(false);
  const workspace = str(workspaces.find(item => workspaceSlugOf(item) === workspaceSlug)?.name, "Workspace");
  const entity = useResource<Row>(entityPath(section, id));
  const inferredProjectId = section === "project" ? id : projectId || str(entity.data?.project_id, "");
  const project = useResource<Row>(inferredProjectId && section !== "project" ? `/api/projects/${inferredProjectId}` : null);
  const projectName = section === "project" ? str(entity.data?.title ?? entity.data?.name, "") : str(project.data?.title ?? project.data?.name, "");
  const entityName = section !== "project" ? str(entity.data?.title ?? entity.data?.name, "") : "";
  const fallback = !projectName && !entityName ? routeLabel(section) : "";
  useEffect(() => {
    const parts = [entityName || projectName || fallback, workspace, "Modelfiche"].filter(Boolean);
    document.title = parts.join(" - ");
  }, [entityName, fallback, projectName, workspace]);
  const s3ImportHref = workspaceHref("import", { source: "s3", project: projectId }, workspaceSlug);
  const localImportHref = workspaceHref("import", { source: "local", project: projectId }, workspaceSlug);
  return <header className="vela-topbar">
    <button ref={navOpenerRef} className="vela-icon-button vela-responsive-nav" type="button" aria-label="Open navigation" aria-controls="vela-navigation" aria-expanded={false} onClick={onOpenNavigation}><Menu size={18} /></button>
    <div className="vela-breadcrumbs"><span className="vela-workspace-label">Workspace:</span><div className="vela-workspace-switcher"><Dropdown aria-label="Workspace" value={workspaceSlug} options={workspaces.map(item => ({ value: workspaceSlugOf(item), label: str(item.name, "Workspace") }))} onChange={value => {
      if (!value || value === workspaceSlug) return;
      if (hasDirtyForm() && !window.confirm("Discard this form and switch workspaces?")) return;
      const nextWorkspace = workspaces.find(item => workspaceSlugOf(item) === value);
      if (!nextWorkspace) return;
      setActiveWorkspaceId(idOf(nextWorkspace));
      window.location.hash = workspaceHref("dashboard", {}, value);
    }} /></div><span className="vela-entity-breadcrumb">{projectName && <>&nbsp; / &nbsp;<strong>{projectName}</strong></>}{entityName && entityName !== projectName && <>&nbsp; / &nbsp;{entityName}</>}{fallback && <>&nbsp; / &nbsp;{fallback}</>}</span></div>
    <GlobalSearch />
    <div className="vela-create-wrap" onKeyDown={event => { if (event.key === "Escape") setCreateOpen(false); }}>
      <button className="vela-create-button" type="button" aria-label="Create or import" aria-haspopup="menu" aria-expanded={createOpen} onClick={() => setCreateOpen(!createOpen)}><Plus size={20} aria-hidden="true" /></button>
      {createOpen && <div className="vela-create-menu" role="menu">
        <CreateLink href={workspaceHref("dashboard", { create: "project" }, workspaceSlug)} icon={<Folder size={15} />} onSelect={() => setCreateOpen(false)}>Project</CreateLink>
        <CreateLink href={workspaceHref("generate", { workflow: "image" }, workspaceSlug)} icon={<ImagePlus size={15} />} onSelect={() => setCreateOpen(false)}>Image</CreateLink>
        <CreateLink href={workspaceHref("transfers", { mode: "import", type: "dataset" }, workspaceSlug)} icon={<Images size={15} />} onSelect={() => setCreateOpen(false)}>Dataset</CreateLink>
        <CreateLink href={workspaceHref("models", {}, workspaceSlug)} icon={<Box size={15} />} onSelect={() => setCreateOpen(false)}>Model</CreateLink>
        <CreateLink href={workspaceHref("grids", {}, workspaceSlug)} icon={<LayoutDashboard size={15} />} onSelect={() => setCreateOpen(false)}>Grid</CreateLink>
        <CreateLink href={s3ImportHref} icon={<ArrowDownToLine size={15} />} onSelect={() => setCreateOpen(false)}>S3 import</CreateLink>
        <CreateLink href={localImportHref} icon={<Folder size={15} />} onSelect={() => setCreateOpen(false)}>Local folder import</CreateLink>
        <CreateLink href={workspaceHref("transfers", { mode: "import" }, workspaceSlug)} icon={<ArrowDownToLine size={15} />} onSelect={() => setCreateOpen(false)}>Queue import (Transfers)</CreateLink>
      </div>}
    </div>
    <ProfileSelect compact workspaceName={workspace} />
    <button className="vela-icon-button vela-theme-toggle" type="button" aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`} aria-pressed={theme === "dark"} title={`Use ${theme === "dark" ? "light" : "dark"} mode`} onClick={onToggleTheme}>{theme === "dark" ? <Sun size={17} aria-hidden="true" /> : <Moon size={17} aria-hidden="true" />}</button>
    <button ref={inspectorOpenerRef} className="vela-icon-button vela-responsive-inspector" type="button" aria-label="Open metadata and notes" aria-controls="vela-metadata" aria-expanded={false} onClick={onOpenInspector}><PanelRightOpen size={18} /></button>
  </header>;
}

function CreateLink({ href, icon, onSelect, children }: { href: string; icon: ReactNode; onSelect: () => void; children: ReactNode }) {
  return <a role="menuitem" href={href} onClick={onSelect}>{icon}<span>{children}</span></a>;
}

function hasDirtyForm(): boolean {
  return Array.from(document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>("form input, form textarea, form select")).some(field => {
    if (field instanceof HTMLInputElement && (field.type === "checkbox" || field.type === "radio")) {
      return field.checked !== field.defaultChecked;
    }
    if (field instanceof HTMLSelectElement) {
      return Array.from(field.options).some(option => option.selected !== option.defaultSelected);
    }
    return field.value !== field.defaultValue;
  });
}

function GlobalSearch() {
  const [value, setValue] = useState("");
  const [activeIndex, setActiveIndex] = useState(-1);
  const resource = useResource<Row>(value.trim().length >= 2 ? `/api/search${query({ q: value.trim(), limit: 8 })}` : null);
  const results: Array<Row & { preview_asset_id: string }> = rows(resource.data?.results).map((result: Row) => ({ ...result, preview_asset_id: str(result.preview_asset_id, "") }));
  const open = value.trim().length >= 2;
  useEffect(() => { setActiveIndex(-1); }, [value]);
  const selectResult = (index: number) => {
    const result = results[index];
    if (!result) return;
    window.location.hash = searchHref(result).replace(/^#/, "");
    setValue("");
  };
  const resultContent = resource.loading
    ? <span role="status" aria-live="polite">Searching workspace...</span>
    : resource.error
      ? <span role="alert">{resource.error}</span>
      : results.length
        ? results.map((result, index) => <a id={`global-search-option-${index}`} role="option" aria-selected={activeIndex === index} href={searchHref(result)} key={`${str(result.type)}-${idOf(result)}`} onMouseEnter={() => setActiveIndex(index)} onClick={() => setValue("")}>{result.preview_asset_id && <AssetImage assetRevisionId={result.preview_asset_id} alt="" loading="lazy" />}<span><strong>{str(result.title)}</strong><small>{str(result.type)} · {str(result.subtitle)}</small></span></a>)
        : <span role="status" aria-live="polite">No matching workspace objects</span>;
  return <div className="vela-global-search"><Search size={16} aria-hidden="true" /><label className="sr-only" htmlFor="global-search">Search workspace</label><input id="global-search" className="vela-search" role="combobox" aria-autocomplete="list" aria-expanded={open} aria-controls="global-search-results" aria-activedescendant={activeIndex >= 0 ? `global-search-option-${activeIndex}` : undefined} value={value} onChange={event => setValue(event.target.value)} onKeyDown={event => {
    if (event.key === "Escape") { setValue(""); setActiveIndex(-1); }
    else if (event.key === "ArrowDown" && results.length) { event.preventDefault(); setActiveIndex(index => (index + 1) % results.length); }
    else if (event.key === "ArrowUp" && results.length) { event.preventDefault(); setActiveIndex(index => (index - 1 + results.length) % results.length); }
    else if (event.key === "Enter" && activeIndex >= 0) { event.preventDefault(); selectResult(activeIndex); }
  }} placeholder="Search projects, runs, model files..." />{open && <div className="vela-search-results" id="global-search-results" role="listbox" aria-label="Workspace search results">{resultContent}</div>}</div>;
}
function Sidebar({ section, id, activeProjectId, projects, workspaceSlug, theme, collapsed, mobileOpen, restoreFocusRef, onMobileClose, onCollapse }: { section: string; id: string; activeProjectId: string; projects: Row[]; workspaceSlug: string; theme: ThemeMode; collapsed: boolean; mobileOpen: boolean; restoreFocusRef: RefObject<HTMLButtonElement | null>; onMobileClose: () => void; onCollapse: () => void }) {
  const sources = useResource<unknown>("/api/import-sources");
  const visibleProjects = projects.filter(project => project.state !== "archived");
  const shownProjects = visibleProjects.length ? visibleProjects : projects;
  const activeSources = rows(sources.data).filter(source => source.is_active !== false).length;
  const containerRef = useRef<HTMLElement>(null);
  useFocusWorkspace({ active: mobileOpen, onDismiss: onMobileClose, containerRef, restoreFocusRef });
  return <aside ref={containerRef} className="vela-sidebar" id="vela-navigation" data-mobile-open={mobileOpen} role={mobileOpen ? "dialog" : undefined} aria-modal={mobileOpen ? "true" : undefined} aria-labelledby={mobileOpen ? "vela-navigation-title" : undefined} onClick={event => { if ((event.target as HTMLElement).closest("a")) onMobileClose(); }}>
    <h2 id="vela-navigation-title" className="sr-only">Primary navigation</h2>
    <div className="vela-brand"><a className="vela-wordmark" href={workspaceHref("dashboard", {}, workspaceSlug)} aria-label="ModelFiche dashboard"><img src={`/assets/modelfiche-logo-${theme}.png`} alt="ModelFiche" /></a><button type="button" className="vela-icon-button vela-desktop-rail-control" aria-label={collapsed ? "Expand navigation" : "Collapse navigation"} aria-controls="vela-navigation" aria-expanded={!collapsed} onClick={onCollapse}>{collapsed ? <ChevronRight size={17} /> : <PanelLeftClose size={17} />}</button><button type="button" className="vela-icon-button vela-mobile-close" aria-label="Close navigation" onClick={onMobileClose}><X size={18} /></button></div>
    <nav className="vela-nav" aria-label="Primary navigation" data-layout={SHELL_LAYOUT_CONTRACT.navigation}>
      <VelaNavLink href={workspaceHref("dashboard", {}, workspaceSlug)} active={section === "dashboard" || section === "projects"} icon={<LayoutDashboard size={16} />}>Dashboard</VelaNavLink>
      <VelaNavLink href={workspaceHref("gallery", {}, workspaceSlug)} active={section === "gallery" || section === "image"} icon={<Images size={16} />}>Gallery</VelaNavLink>
      <div className="vela-nav-group">Projects</div>
      <Notice empty={!shownProjects.length} />
      {shownProjects.map(project => <ProjectTree key={idOf(project)} project={project} workspaceSlug={workspaceSlug} section={section} activeId={id} activeProjectId={activeProjectId} />)}
      <div className="vela-nav-group">Operations</div>
      <VelaNavLink href={workspaceHref("generation-queue", {}, workspaceSlug)} active={section === "generation-queue"} icon={<List size={16} />}>Queue</VelaNavLink>
      <VelaNavLink href={workspaceHref("transfers", {}, workspaceSlug)} active={section === "transfers" || section === "import" || section === "jobs"} icon={<ArrowLeftRight size={16} />}>Transfers</VelaNavLink>
    </nav>
    <div className="vela-sidebar-footer" data-layout={SHELL_LAYOUT_CONTRACT.footer}>
      <VelaNavLink href={workspaceHref("docs", {}, workspaceSlug)} active={section === "docs"} icon={<BookOpen size={16} />}>Docs</VelaNavLink>
      <VelaNavLink href={workspaceHref("settings", {}, workspaceSlug)} active={section === "settings"} icon={<Settings size={16} />}>Settings</VelaNavLink>
      <div className="vela-storage"><strong><Database size={14} /> Storage</strong><span className="vela-meta">Local cache · {sources.loading ? "checking sources" : `${activeSources} S3 source${activeSources === 1 ? "" : "s"}`}</span></div>
    </div>
    <button type="button" className="vela-rail-label" aria-label="Expand navigation" aria-controls="vela-navigation" aria-expanded={!collapsed} onClick={onCollapse}>NAVIGATION</button>
  </aside>;
}

function VelaNavLink({ href, active, icon, children }: { href: string; active: boolean; icon: ReactNode; children: ReactNode }) {
  return <a className="vela-nav-item" href={href} aria-current={active ? "page" : undefined}><span className="vela-nav-icon">{icon}</span><span className="vela-nav-text">{children}</span></a>;
}
function ProjectTree({ project, workspaceSlug, section, activeId, activeProjectId }: { project: Row; workspaceSlug: string; section: string; activeId: string; activeProjectId: string }) {
  const projectId = idOf(project);
  const [expanded, setExpanded] = useState(((section === "project" && activeId === projectId) || activeProjectId === projectId));
  useEffect(() => { if ((section === "project" && activeId === projectId) || activeProjectId === projectId) setExpanded(true); }, [section, activeId, activeProjectId, projectId]);
  // A collapsed project must stay cheap. Eagerly loading three collections for
  // every project made route navigation contend with dozens of sidebar calls.
  const datasets = useResource<unknown>(expanded ? `/api/datasets${query({ project_id: projectId })}` : null);
  const models = useResource<unknown>(expanded ? `/api/models${query({ project_id: projectId })}` : null);
  const grids = useResource<unknown>(expanded ? `/api/projects/${projectId}/grids` : null);
  return <div className={`vela-project-nav ${project.state === "archived" ? "is-archived" : ""}`}>
    <div className="vela-project-nav-root"><a className="vela-nav-item" aria-current={(activeProjectId === projectId || (section === "project" && activeId === projectId)) ? "page" : undefined} href={workspaceHref(`project/${projectId}`, {}, workspaceSlug)}><span className="vela-nav-icon"><Folder size={15} /></span><span className="vela-nav-text">{str(project.title ?? project.name)}</span></a><button type="button" className="vela-icon-button" onClick={() => setExpanded(!expanded)} aria-label={`${expanded ? "Collapse" : "Expand"} ${str(project.title)}`} aria-expanded={expanded}>{expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</button></div>
    {expanded && <div className="vela-project-tree">
      <ActiveTrainingNavigation projectId={projectId} />
      <NavBranch label="Models" href={workspaceHref("models", { project: projectId }, workspaceSlug)} items={rows(models.data)} itemRoute="model" projectId={projectId} workspaceSlug={workspaceSlug} section={section} activeId={activeId} activeProjectId={activeProjectId} />
      <NavBranch label="Datasets" href={workspaceHref("datasets", { project: projectId }, workspaceSlug)} items={rows(datasets.data)} itemRoute="dataset" projectId={projectId} workspaceSlug={workspaceSlug} section={section} activeId={activeId} activeProjectId={activeProjectId} />
      <NavBranch label="Grids" href={workspaceHref("grids", { project: projectId }, workspaceSlug)} items={rows(grids.data).filter(row => str(row.project_id, "") === projectId || "plan" in row || "plan_digest" in row || "cells" in row)} itemRoute="grid" projectId={projectId} workspaceSlug={workspaceSlug} section={section} activeId={activeId} activeProjectId={activeProjectId} />
    </div>}
  </div>;
}

function NavBranch({ label, href, items, itemRoute, projectId, workspaceSlug, section, activeId, activeProjectId }: { label: string; href: string; items: Row[]; itemRoute: string; projectId: string; workspaceSlug: string; section: string; activeId: string; activeProjectId: string }) {
  const groupActive = activeProjectId === projectId && (section === `${itemRoute}s` || (itemRoute === "eval" && section === "evals"));
  return <div className="vela-tree-group"><a className="vela-nav-group" href={href} aria-current={groupActive ? "page" : undefined}>{label}</a>{items.slice(0, 5).map(item => { const itemId = idOf(item); const active = activeProjectId === projectId && section === itemRoute && activeId === itemId; return <a className="vela-nav-item" key={itemId} href={workspaceHref(`${itemRoute}/${itemId}`, { project: projectId }, workspaceSlug)} aria-current={active ? "page" : undefined}><span className="vela-nav-text">{str(item.name ?? item.title, "Untitled")}</span></a>; })}{items.length > 5 && <a className="vela-nav-item vela-nav-show-all" href={href}><span className="vela-nav-text">View all {items.length} {label.toLowerCase()}</span></a>}</div>;
}
function activityPreviewHref(item: Row, previewId: string, objectHref: string | null, projectId: string) {
  const parsedObject = parseWorkspaceHash(objectHref ?? "");
  const [objectPath, rawQuery = ""] = parsedObject.route.split("?", 2);
  const params = new URLSearchParams(rawQuery);
  const details = item.details && typeof item.details === "object" ? item.details as Row : {};
  const evalPathId = objectPath.startsWith("eval/") ? decodeURIComponent(objectPath.slice("eval/".length)) : "";
  const evalRunId = evalPathId || str(item.eval_run_id ?? details.eval_run_id, params.get("eval") ?? "");
  const sourceProjectId = str(item.project_id, projectId || params.get("project") || "");
  return galleryOpenHref({
    asset_revision_id: previewId,
    project_id: sourceProjectId,
    eval_run_id: evalRunId,
    origin_type: evalRunId ? "EVAL" : str(item.origin_type ?? details.origin_type, ""),
  });
}

function ContextInspector({ section, id, projectId, collapsed, mobileOpen, restoreFocusRef, onMobileClose, onCollapse }: { section: string; id: string; projectId: string; collapsed: boolean; mobileOpen: boolean; restoreFocusRef: RefObject<HTMLButtonElement | null>; onMobileClose: () => void; onCollapse: () => void }) {
  const entity = useResource<Row>(entityPath(section, id));
  const subject = inspectorSubject(section, id);
  const activity = useResource<unknown>(`/api/activity${query({ project_id: projectId || undefined, subject_type: section === "project" ? undefined : subject?.type, subject_id: section === "project" ? undefined : subject?.id, limit: 20 })}`);
  const activityItems = rows(activity.data).filter(item => str(item.action ?? item.event_type).toLowerCase() !== "asset.deleted");
  useEffect(() => {
    const refresh = () => void activity.reload();
    window.addEventListener("modelfiche:activity-changed", refresh);
    return () => window.removeEventListener("modelfiche:activity-changed", refresh);
  }, [projectId, section, id]);
  const title = entityTitle(section, entity.data);
  const containerRef = useRef<HTMLElement>(null);
  useFocusWorkspace({ active: mobileOpen, onDismiss: onMobileClose, containerRef, restoreFocusRef });
  const metadataSelection = useDragTextSelection();
  return <aside {...metadataSelection} ref={containerRef} className="vela-inspector vela-copyable-metadata" id="vela-metadata" data-mobile-open={mobileOpen} data-layout={SHELL_LAYOUT_CONTRACT.inspector} role={mobileOpen ? "dialog" : undefined} aria-modal={mobileOpen ? "true" : undefined} aria-labelledby={mobileOpen ? "vela-metadata-title" : undefined}>
    <img className="mf-rail-ornament mf-rail-ornament-light" src="/assets/light-cobalt-xerox-texture.svg" alt="" aria-hidden="true" />
    <img className="mf-rail-ornament mf-rail-ornament-dark" src="/assets/dark-celestial-catalogue-labels.svg" alt="" aria-hidden="true" />
    <div className="vela-inspector-heading"><h2 id="vela-metadata-title">Metadata</h2><button type="button" className="vela-icon-button vela-desktop-rail-control" aria-label={collapsed ? "Expand metadata" : "Collapse metadata"} aria-controls="vela-metadata" aria-expanded={!collapsed} onClick={onCollapse}>{collapsed ? <ChevronLeft size={17} /> : <PanelRightClose size={17} />}</button><button type="button" className="vela-icon-button vela-mobile-close" aria-label="Close metadata and notes" onClick={onMobileClose}><X size={18} /></button></div>
    <section className="vela-inspector-card"><div className="vela-context-title"><div><h3>{title}</h3>{section !== "project" && <Status value={str(entity.data?.state ?? entity.data?.status, section === "dashboard" ? "workspace" : "selected")} />}</div>{entity.data && <CopyMetadataButton value={entity.data} />}</div><Notice error={entity.error} loading={entity.loading} />{entity.data ? <MetadataRows value={entity.data} /> : <p className="vela-muted-copy">Select an object to inspect its metadata and history.</p>}</section>
    {subject && ["project", "dataset", "model"].includes(subject.type) && <NotesPanel subjectType={subject.type} subjectId={subject.id} />}
    <section className="vela-inspector-card"><div className="vela-section-heading"><h2>{projectId ? "Project activity" : "Workspace activity"}</h2></div><Notice error={activity.error} loading={activity.loading} empty={!activityItems.length} /><div className="vela-activity-list">{activityItems.map(item => {
      const rawEvent = str(item.action ?? item.event_type);
      const eventLabel = activityLabel(rawEvent);
      const objectHref = contextualActivityHref(item.object_href, projectId);
      const previewId = previewAssetId(item.preview_asset_id);
      const objectLabel = activityDisplayText(item.object_label, previewId ? "Generated image" : "Activity");
      const summary = activityDisplayText(item.summary ?? item.subject_type, previewId ? objectLabel : eventLabel);
      const profileName = str(item.profile_name, "");
      const summaryContent = objectLabel && objectLabel !== summary ? <>{objectLabel} · {summary}</> : objectLabel || summary;
      const previewHref = previewId ? activityPreviewHref(item, previewId, objectHref, projectId) : null;
      const activityThumb = <div className="vela-activity-thumb">{previewId ? <AssetImage assetRevisionId={previewId} alt={objectLabel || summary} style={{ width: "100%", height: "100%", objectFit: "cover" }} /> : <Activity size={17} />}</div>;
      const activityText = <div><h3>{eventLabel}</h3><div className="vela-activity-copy"><p className="vela-activity-summary">{summaryContent}</p>{profileName && <span className="vela-meta">by {profileName}</span>}<time className="vela-activity-time" dateTime={str(item.created_at)}>{formatDate(item.created_at)}</time></div></div>;
      const primaryContent = <>{activityThumb}{activityText}</>;
      return <article className="vela-activity-item" key={idOf(item)}>{previewHref && <a className="vela-activity-preview" href={previewHref} aria-label={`Open ${objectLabel || summary} in gallery`}>{activityThumb}</a>}{objectHref ? <a className={previewHref ? "vela-activity-link vela-activity-text-link" : "vela-activity-link"} href={objectHref} aria-label={objectLabel || summary}>{previewHref ? activityText : primaryContent}</a> : previewHref ? activityText : primaryContent}</article>;
    })}</div></section>
    <button type="button" className="vela-rail-label" aria-label="Expand metadata" aria-controls="vela-metadata" aria-expanded={!collapsed} onClick={onCollapse}>METADATA</button>
  </aside>;
}

function contextualActivityHref(value: unknown, projectId: string) {
  const href = activityObjectHref(value);
  if (!href) return null;
  const [path, rawQuery = ""] = href.split("?", 2);
  const params = new URLSearchParams(rawQuery);
  if (projectId && !params.has("project") && !path.startsWith("#/project/")) params.set("project", projectId);
  const queryString = params.toString();
  return canonicalWorkspaceHref(`${path}${queryString ? `?${queryString}` : ""}`, routeWorkspaceSlug());
}
function NotesPanel({ subjectType, subjectId }: { subjectType: string; subjectId: string }) {
  const resource = useResource<unknown>(`/api/notes${query({ subject_type: subjectType, subject_id: subjectId })}`);
  const [body, setBody] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const notes = rows(resource.data);
  return <section className="vela-inspector-card vela-notes-card"><div className="vela-section-heading"><h2>Notes</h2><span className="vela-meta">{notes.length} previous</span></div><div className="vela-note-tools" aria-label="Markdown formatting"><button type="button" title="Bold" onClick={() => setBody(`${body}**bold**`)} aria-label="Bold"><Bold size={14} /></button><button type="button" title="Italic" onClick={() => setBody(`${body}_italic_`)} aria-label="Italic"><Italic size={14} /></button><button type="button" title="Bulleted list" onClick={() => setBody(`${body}\n- `)} aria-label="Bulleted list"><List size={14} /></button></div><textarea aria-label="Markdown note" value={body} onChange={event => setBody(event.target.value)} placeholder="Write markdown note..." />{error && <div className="vela-notice vela-notice-error" role="alert">{error}</div>}<button className="vela-button vela-button-primary vela-button-block" disabled={!body.trim() || saving} title={!body.trim() ? "Write a note before adding it." : undefined} onClick={async () => { setSaving(true); setError(""); try { await api("/api/notes", jsonBody({ subject_type: subjectType, subject_id: subjectId, body })); setBody(""); await resource.reload(); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); } finally { setSaving(false); } }}>{saving ? "Adding..." : "Add note"}</button><div className="vela-note-history">{notes.map(note => <button key={idOf(note)} type="button" title="Load this note into the editor" onClick={() => setBody(str(note.body ?? note.text, ""))}><span>{formatDate(note.created_at)}</span>{str(note.body ?? note.text)}</button>)}</div></section>;
}

function MetadataRows({ value }: { value: Row }) {
  if (value.counts && typeof value.counts === "object") return <dl className="vela-context-stats">{Object.entries(value.counts as Row).map(([key, item]) => <div className="vela-stat" key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{str(item)}</dd></div>)}</dl>;
  if (value.llm && value.fal) {
    const llm = value.llm as Row;
    const credentials = (value.credentials ?? {}) as Row;
    return <dl className="vela-metadata-list">
      <Metadata label="LLM" value={`${str(llm.provider)} / ${str(llm.model, "not set")}`} />
      <Metadata label="FAL" value={credentials.fal_configured ? "configured" : "not configured"} />
    </dl>;
  }
  const keys = ["title", "name", "kind", "status", "state", "step", "base_model", "trainer", "lifecycle_state", "source_prefix", "created_at"];
  return <dl className="vela-metadata-list">{keys.filter(key => value[key] !== undefined && value[key] !== null).slice(0, 7).map(key => <Metadata key={key} label={key.replaceAll("_", " ")} value={Array.isArray(value[key]) ? value[key].map(String).join(", ") : str(value[key])} />)}</dl>;
}

function Metadata({ label, value }: { label: string; value: string }) { return <div><dt>{label}</dt><dd>{value}</dd></div>; }
function projectForRoute(section: string, id: string, params: URLSearchParams) { return section === "project" ? id : params.get("project") ?? ""; }
function inspectorSubject(section: string, id: string) { const map: Record<string, string> = { project: "project", dataset: "dataset", model: "model", run: "training_run", checkpoint: "checkpoint", eval: "eval_run", grid: "grid_definition", image: "asset", jobs: "import_job" }; return id && map[section] ? { type: map[section], id } : null; }
function entityPath(section: string, id: string): string | null { if (section === "dashboard" || section === "projects") return "/api/dashboard"; if (section === "settings") return "/api/operator-settings"; if (!id) return null; const map: Record<string, string> = { project: "projects", dataset: "datasets", model: "models", run: "runs", checkpoint: "checkpoints", eval: "eval-runs", grid: "grids", image: "assets", jobs: "jobs" }; return map[section] ? `/api/${map[section]}/${id}` : null; }
function entityTitle(section: string, row: Row | null) { return str(row?.title ?? row?.name, routeLabel(section)); }
function routeLabel(section: string) { return ({ dashboard: "Dashboard", projects: "Dashboard", project: "Project", gallery: "Gallery", image: "Image", datasets: "Datasets", dataset: "Dataset", runs: "Training run", run: "Training run", checkpoint: "Checkpoint", models: "Models", model: "Model", evals: "Eval generator", eval: "Eval viewer", grids: "Grid generator", grid: "Grid viewer", transfers: "Transfers", import: "Transfers", jobs: "Transfer job", settings: "Settings", reviews: "Reviews" } as Record<string, string>)[section] ?? section; }
function formatDate(value: unknown) { const date = new Date(str(value, "")); return Number.isNaN(date.valueOf()) ? str(value) : date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); }
function searchHref(result: Row) { const type = str(result.type); const route = ({ project: "project", asset: "image", dataset: "dataset", run: "run", model: "model" } as Record<string, string>)[type] ?? "gallery"; return `#/${route}/${idOf(result)}${routeQuery({ project: result.project_id })}`; }
function useStoredBoolean(key: string) { const [value, setValue] = useState(() => localStorage.getItem(key) === "true"); const update = (next: boolean) => { localStorage.setItem(key, String(next)); setValue(next); }; return [value, update] as const; }
