import { useEffect, useState, type ReactNode } from "react";
import {
  Activity,
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
  Italic,
  LayoutDashboard,
  List,
  Menu,
  PanelLeftClose,
  PanelRightClose,
  PanelRightOpen,
  Plus,
  Search,
  Settings,
  Sparkles,
  X,
} from "lucide-react";
import { api, idOf, jsonBody, listOf, query, str } from "./api";
import { useResource } from "./hooks";
import { Notice, ProfileSelect, Status } from "./ui";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);

export function WorkbenchShell({ section, id, params, children }: { section: string; id: string; params: URLSearchParams; children: ReactNode }) {
  const [leftCollapsed, setLeftCollapsed] = useStoredBoolean("titles.leftCollapsed");
  const [rightCollapsed, setRightCollapsed] = useStoredBoolean("titles.rightCollapsed");
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [mobileInspectorOpen, setMobileInspectorOpen] = useState(false);
  const projects = useResource<unknown>("/api/projects?limit=100");
  const workspaces = useResource<unknown>("/api/workspaces");
  const projectId = projectForRoute(section, id, params);
  useEffect(() => { const close = (event: KeyboardEvent) => { if (event.key === "Escape") { setMobileNavOpen(false); setMobileInspectorOpen(false); } }; window.addEventListener("keydown", close); return () => window.removeEventListener("keydown", close); }, []);
  useEffect(() => { setMobileNavOpen(false); setMobileInspectorOpen(false); }, [section, id]);
  return <div className="vela-shell" data-nav-collapsed={leftCollapsed} data-inspector-collapsed={rightCollapsed}>
    <Sidebar section={section} id={id} activeProjectId={projectId} projects={rows(projects.data)} collapsed={leftCollapsed} mobileOpen={mobileNavOpen} onMobileClose={() => setMobileNavOpen(false)} onCollapse={() => setLeftCollapsed(!leftCollapsed)} />
    <div className="vela-workspace">
      <Topbar section={section} workspaces={rows(workspaces.data)} onOpenNavigation={() => { setLeftCollapsed(false); setMobileInspectorOpen(false); setMobileNavOpen(true); }} onOpenInspector={() => { setRightCollapsed(false); setMobileNavOpen(false); setMobileInspectorOpen(true); }} />
      <div className="vela-route-stage">{children}</div>
    </div>
    <ContextInspector section={section} id={id} projectId={projectId} collapsed={rightCollapsed} mobileOpen={mobileInspectorOpen} onMobileClose={() => setMobileInspectorOpen(false)} onCollapse={() => setRightCollapsed(!rightCollapsed)} />
    {(mobileNavOpen || mobileInspectorOpen) && <button className="vela-drawer-scrim" aria-label="Close open panel" onClick={() => { setMobileNavOpen(false); setMobileInspectorOpen(false); }} />}
  </div>;
}

function Topbar({ section, workspaces, onOpenNavigation, onOpenInspector }: { section: string; workspaces: Row[]; onOpenNavigation: () => void; onOpenInspector: () => void }) {
  const [createOpen, setCreateOpen] = useState(false);
  const workspace = str(workspaces[0]?.name, "Titles XYZ");
  return <header className="vela-topbar">
    <button className="vela-icon-button vela-responsive-nav" type="button" aria-label="Open navigation" onClick={onOpenNavigation}><Menu size={18} /></button>
    <div className="vela-breadcrumbs">Workspaces&nbsp; / &nbsp;<strong>{workspace}</strong>&nbsp; / &nbsp;{routeLabel(section)}</div>
    <GlobalSearch />
    <div className="vela-create-wrap" onKeyDown={event => { if (event.key === "Escape") setCreateOpen(false); }}>
      <button className="vela-create-button" type="button" aria-label="Create or import" aria-haspopup="menu" aria-expanded={createOpen} onClick={() => setCreateOpen(!createOpen)}><Plus size={20} /></button>
      {createOpen && <div className="vela-create-menu" role="menu">
        <CreateLink href="#/dashboard?create=project" icon={<Folder size={15} />} onSelect={() => setCreateOpen(false)}>Project</CreateLink>
        <CreateLink href="#/transfers?mode=import&type=dataset" icon={<Images size={15} />} onSelect={() => setCreateOpen(false)}>Dataset</CreateLink>
        <CreateLink href="#/models" icon={<Box size={15} />} onSelect={() => setCreateOpen(false)}>Model</CreateLink>
        <CreateLink href="#/evals" icon={<Sparkles size={15} />} onSelect={() => setCreateOpen(false)}>Eval</CreateLink>
        <CreateLink href="#/grids" icon={<LayoutDashboard size={15} />} onSelect={() => setCreateOpen(false)}>Grid</CreateLink>
        <CreateLink href="#/transfers?mode=import" icon={<ArrowDownToLine size={15} />} onSelect={() => setCreateOpen(false)}>Import</CreateLink>
      </div>}
    </div>
    <ProfileSelect compact />
    <button className="vela-icon-button vela-responsive-inspector" type="button" aria-label="Open metadata and notes" onClick={onOpenInspector}><PanelRightOpen size={18} /></button>
  </header>;
}

function CreateLink({ href, icon, onSelect, children }: { href: string; icon: ReactNode; onSelect: () => void; children: ReactNode }) {
  return <a role="menuitem" href={href} onClick={onSelect}>{icon}<span>{children}</span></a>;
}

function GlobalSearch() {
  const [value, setValue] = useState("");
  const resource = useResource<Row>(value.trim().length >= 2 ? `/api/search${query({ q: value.trim(), limit: 8 })}` : null);
  const results = rows(resource.data?.results);
  const open = value.trim().length >= 2;
  return <div className="vela-global-search"><Search size={16} aria-hidden="true" /><label className="sr-only" htmlFor="global-search">Search workspace</label><input id="global-search" className="vela-search" role="combobox" aria-autocomplete="list" aria-expanded={open} aria-controls="global-search-results" value={value} onChange={event => setValue(event.target.value)} onKeyDown={event => { if (event.key === "Escape") setValue(""); }} placeholder="Search projects, runs, model files..." />{open && <div className="vela-search-results" id="global-search-results" role="listbox">{resource.loading ? <span>Searching...</span> : resource.error ? <span role="alert">{resource.error}</span> : results.length ? results.map(result => <a role="option" aria-selected="false" href={searchHref(result)} key={`${str(result.type)}-${idOf(result)}`} onClick={() => setValue("")}><strong>{str(result.title)}</strong><span>{str(result.type)} · {str(result.subtitle)}</span></a>) : <span>No matching workspace objects</span>}</div>}</div>;
}

function Sidebar({ section, id, activeProjectId, projects, collapsed, mobileOpen, onMobileClose, onCollapse }: { section: string; id: string; activeProjectId: string; projects: Row[]; collapsed: boolean; mobileOpen: boolean; onMobileClose: () => void; onCollapse: () => void }) {
  const sources = useResource<unknown>("/api/import-sources");
  const visibleProjects = projects.filter(project => project.state !== "archived");
  const shownProjects = visibleProjects.length ? visibleProjects : projects;
  const activeSources = rows(sources.data).filter(source => source.is_active !== false).length;
  return <aside className="vela-sidebar" id="vela-navigation" data-mobile-open={mobileOpen} onClick={event => { if ((event.target as HTMLElement).closest("a")) onMobileClose(); }}>
    <div className="vela-brand"><a className="vela-wordmark" href="#/dashboard">VELA</a><button type="button" className="vela-icon-button vela-desktop-rail-control" aria-label={collapsed ? "Expand navigation" : "Collapse navigation"} aria-controls="vela-navigation" aria-expanded={!collapsed} onClick={onCollapse}>{collapsed ? <ChevronRight size={17} /> : <PanelLeftClose size={17} />}</button><button type="button" className="vela-icon-button vela-mobile-close" aria-label="Close navigation" onClick={onMobileClose}><X size={18} /></button></div>
    <nav className="vela-nav" aria-label="Primary navigation">
      <VelaNavLink href="#/dashboard" active={section === "dashboard" || section === "projects"} icon={<LayoutDashboard size={16} />}>Dashboard</VelaNavLink>
      <VelaNavLink href="#/gallery" active={section === "gallery" || section === "image"} icon={<Images size={16} />}>Gallery</VelaNavLink>
      <div className="vela-nav-group">Projects</div>
      <Notice empty={!shownProjects.length} />
      {shownProjects.map(project => <ProjectTree key={idOf(project)} project={project} section={section} activeId={id} activeProjectId={activeProjectId} />)}
      <div className="vela-nav-group">Operations</div>
      <VelaNavLink href="#/transfers" active={section === "transfers" || section === "import" || section === "jobs"} icon={<ArrowLeftRight size={16} />}>Transfers</VelaNavLink>
    </nav>
    <div className="vela-sidebar-footer">
      <VelaNavLink href="#/settings" active={section === "settings"} icon={<Settings size={16} />}>Settings</VelaNavLink>
      <div className="vela-storage"><strong><Database size={14} /> Storage</strong><span className="vela-meta">Local cache · {sources.loading ? "checking sources" : `${activeSources} S3 source${activeSources === 1 ? "" : "s"}`}</span></div>
    </div>
    <button type="button" className="vela-rail-label" aria-label="Expand navigation" aria-controls="vela-navigation" aria-expanded={!collapsed} onClick={onCollapse}>NAVIGATION</button>
  </aside>;
}

function VelaNavLink({ href, active, icon, children }: { href: string; active: boolean; icon: ReactNode; children: ReactNode }) {
  return <a className="vela-nav-item" href={href} aria-current={active ? "page" : undefined}><span className="vela-nav-icon">{icon}</span><span className="vela-nav-text">{children}</span></a>;
}

function ProjectTree({ project, section, activeId, activeProjectId }: { project: Row; section: string; activeId: string; activeProjectId: string }) {
  const projectId = idOf(project);
  const [expanded, setExpanded] = useState((section === "project" && activeId === projectId) || activeProjectId === projectId);
  // A collapsed project must stay cheap. Eagerly loading three collections for
  // every project made route navigation contend with dozens of sidebar calls.
  const datasets = useResource<unknown>(expanded ? `/api/datasets${query({ project_id: projectId })}` : null);
  const models = useResource<unknown>(expanded ? `/api/models${query({ project_id: projectId })}` : null);
  const evals = useResource<unknown>(expanded ? `/api/eval-runs${query({ project_id: projectId })}` : null);
  return <div className={`vela-project-nav ${project.state === "archived" ? "is-archived" : ""}`}>
    <div className="vela-project-nav-root"><a className="vela-nav-item" aria-current={section === "project" && activeId === projectId ? "page" : undefined} href={`#/project/${projectId}`}><span className="vela-nav-icon"><Folder size={15} /></span><span className="vela-nav-text">{str(project.title ?? project.name)}</span></a><button type="button" className="vela-icon-button" onClick={() => setExpanded(!expanded)} aria-label={`${expanded ? "Collapse" : "Expand"} ${str(project.title)}`} aria-expanded={expanded}>{expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</button></div>
    {expanded && <div className="vela-project-tree">
      <NavBranch label="Models" href={`#/models?project=${projectId}`} items={rows(models.data)} itemRoute="model" projectId={projectId} />
      <NavBranch label="Datasets" href={`#/datasets?project=${projectId}`} items={rows(datasets.data)} itemRoute="dataset" projectId={projectId} />
      <NavBranch label="Evals" href={`#/evals?project=${projectId}`} items={rows(evals.data)} itemRoute="eval" projectId={projectId} />
    </div>}
  </div>;
}

function NavBranch({ label, href, items, itemRoute, projectId }: { label: string; href: string; items: Row[]; itemRoute: string; projectId: string }) {
  return <div className="vela-tree-group"><a className="vela-nav-group" href={href}>{label}</a>{items.slice(0, 5).map(item => <a className="vela-nav-item" key={idOf(item)} href={`#/${itemRoute}/${idOf(item)}?project=${projectId}`}><span className="vela-nav-text">{str(item.name ?? item.title, "Untitled")}</span></a>)}</div>;
}

function ContextInspector({ section, id, projectId, collapsed, mobileOpen, onMobileClose, onCollapse }: { section: string; id: string; projectId: string; collapsed: boolean; mobileOpen: boolean; onMobileClose: () => void; onCollapse: () => void }) {
  const entity = useResource<Row>(entityPath(section, id));
  const subject = inspectorSubject(section, id);
  const activity = useResource<unknown>(`/api/activity${query({ project_id: projectId || undefined, subject_type: subject?.type, subject_id: subject?.id, limit: 20 })}`);
  const title = entityTitle(section, entity.data);
  return <aside className="vela-inspector" id="vela-metadata" data-mobile-open={mobileOpen}>
    <div className="vela-inspector-heading"><h2>Metadata</h2><button type="button" className="vela-icon-button vela-desktop-rail-control" aria-label={collapsed ? "Expand metadata" : "Collapse metadata"} aria-controls="vela-metadata" aria-expanded={!collapsed} onClick={onCollapse}>{collapsed ? <ChevronLeft size={17} /> : <PanelRightClose size={17} />}</button><button type="button" className="vela-icon-button vela-mobile-close" aria-label="Close metadata and notes" onClick={onMobileClose}><X size={18} /></button></div>
    <section className="vela-inspector-card"><div className="vela-context-title"><div><h3>{title}</h3><Status value={str(entity.data?.state ?? entity.data?.status, section === "dashboard" ? "workspace" : "selected")} /></div></div><Notice error={entity.error} loading={entity.loading} />{entity.data ? <MetadataRows value={entity.data} /> : <p className="vela-muted-copy">Select an object to inspect its metadata and history.</p>}</section>
    {subject && ["project", "dataset", "model"].includes(subject.type) && <NotesPanel subjectType={subject.type} subjectId={subject.id} />}
    <section className="vela-inspector-card"><div className="vela-section-heading"><h2>{projectId ? "Project activity" : "Workspace activity"}</h2></div><Notice error={activity.error} loading={activity.loading} empty={!rows(activity.data).length} /><div className="vela-activity-list">{rows(activity.data).map(item => <article className="vela-activity-item" key={idOf(item)}><div className="vela-activity-thumb"><Activity size={17} /></div><div><h3>{str(item.action ?? item.event_type)}</h3><p>{str(item.summary ?? item.subject_type)}<br />{formatDate(item.created_at)}</p></div></article>)}</div></section>
    <button type="button" className="vela-rail-label" aria-label="Expand metadata" aria-controls="vela-metadata" aria-expanded={!collapsed} onClick={onCollapse}>METADATA</button>
  </aside>;
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
  if (value.llm && value.fal) { const llm = value.llm as Row; const fal = value.fal as Row; return <dl className="vela-metadata-list"><Metadata label="LLM" value={`${str(llm.provider)} / ${str(llm.model, "not set")}`} /><Metadata label="FAL" value={str(fal.default_endpoint)} /><Metadata label="Parallel" value={str(fal.max_parallel_jobs)} /></dl>; }
  const keys = ["title", "name", "kind", "status", "state", "step", "base_model", "trainer", "lifecycle_state", "source_prefix", "created_at"];
  return <dl className="vela-metadata-list">{keys.filter(key => value[key] !== undefined && value[key] !== null).slice(0, 7).map(key => <Metadata key={key} label={key.replaceAll("_", " ")} value={Array.isArray(value[key]) ? value[key].map(String).join(", ") : str(value[key])} />)}</dl>;
}

function Metadata({ label, value }: { label: string; value: string }) { return <div><dt>{label}</dt><dd>{value}</dd></div>; }
function projectForRoute(section: string, id: string, params: URLSearchParams) { return section === "project" ? id : params.get("project") ?? ""; }
function inspectorSubject(section: string, id: string) { const map: Record<string, string> = { project: "project", dataset: "dataset", model: "model", run: "training_run", checkpoint: "checkpoint", eval: "eval_run", grid: "grid_definition", image: "asset", jobs: "import_job" }; return id && map[section] ? { type: map[section], id } : null; }
function entityPath(section: string, id: string): string | null { if (section === "dashboard" || section === "projects") return "/api/dashboard"; if (section === "settings") return "/api/operator-settings"; if (!id) return null; const map: Record<string, string> = { project: "projects", dataset: "datasets", model: "models", run: "runs", checkpoint: "checkpoints", eval: "eval-runs", grid: "grids", image: "assets", jobs: "jobs" }; return map[section] ? `/api/${map[section]}/${id}` : null; }
function entityTitle(section: string, row: Row | null) { return str(row?.title ?? row?.name, routeLabel(section)); }
function routeLabel(section: string) { return ({ dashboard: "Dashboard", projects: "Dashboard", project: "Project", gallery: "Gallery", image: "Image", datasets: "Datasets", dataset: "Dataset", runs: "Training run", run: "Training run", checkpoint: "Checkpoint", models: "Models", model: "Model", evals: "Eval generator", eval: "Eval viewer", grids: "Grid generator", grid: "Grid viewer", transfers: "Transfers", import: "Transfers", jobs: "Transfer job", settings: "Settings", "prompt-sets": "Prompt sets", reviews: "Reviews" } as Record<string, string>)[section] ?? section; }
function formatDate(value: unknown) { const date = new Date(str(value, "")); return Number.isNaN(date.valueOf()) ? str(value) : date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); }
function searchHref(result: Row) { const type = str(result.type); const route = ({ project: "project", asset: "image", dataset: "dataset", run: "run", model: "model", prompt_set: "prompt-sets" } as Record<string, string>)[type] ?? "gallery"; return `#/${route}/${idOf(result)}${result.project_id ? `?project=${str(result.project_id)}` : ""}`; }
function useStoredBoolean(key: string) { const [value, setValue] = useState(() => localStorage.getItem(key) === "true"); const update = (next: boolean) => { localStorage.setItem(key, String(next)); setValue(next); }; return [value, update] as const; }
