import { lazy, Suspense, useEffect, useState } from "react";
import { activeWorkspaceId, api, apiUnscoped, ApiError, establishRemoteSession, routeQuery, setActiveWorkspaceId } from "./api";
import { useCanonicalWorkspaceLinks, useHashRoute } from "./hooks";
import { canonicalWorkspaceHref, legacyEntityReference, parseWorkspaceHash } from "./workspace-routing";
import { WorkbenchShell, useThemeController } from "./shell";
import { Field, Form, Panel } from "./ui";

const ImportScreen = lazy(() => import("./screens-core").then(module => ({ default: module.ImportScreen })));
const JobScreen = lazy(() => import("./screens-core").then(module => ({ default: module.JobScreen })));
const ProjectScreen = lazy(() => import("./screens-core").then(module => ({ default: module.ProjectScreen })));
const ProjectsScreen = lazy(() => import("./screens-core").then(module => ({ default: module.ProjectsScreen })));
const SettingsScreen = lazy(() => import("./screens-core").then(module => ({ default: module.SettingsScreen })));
const CheckpointScreen = lazy(() => import("./screens-assets").then(module => ({ default: module.CheckpointScreen })));
const DatasetScreen = lazy(() => import("./screens-assets").then(module => ({ default: module.DatasetScreen })));
const DatasetsScreen = lazy(() => import("./screens-assets").then(module => ({ default: module.DatasetsScreen })));
const GalleryScreen = lazy(() => import("./screens-assets").then(module => ({ default: module.GalleryScreen })));
const ModelScreen = lazy(() => import("./screens-assets").then(module => ({ default: module.ModelScreen })));
const ModelsScreen = lazy(() => import("./screens-assets").then(module => ({ default: module.ModelsScreen })));
const RunScreen = lazy(() => import("./screens-assets").then(module => ({ default: module.RunScreen })));
const RunsScreen = lazy(() => import("./screens-assets").then(module => ({ default: module.RunsScreen })));
const ModelVersionScreen = lazy(() => import("./screens-model-version").then(module => ({ default: module.ModelVersionScreen })));
const EvalComparisonScreen = lazy(() => import("./screens-evals").then(module => ({ default: module.EvalComparisonScreen })));
const EvalScreen = lazy(() => import("./screens-evals").then(module => ({ default: module.EvalScreen })));
const ReviewsScreen = lazy(() => import("./screens-evals").then(module => ({ default: module.ReviewsScreen })));
const GridScreen = lazy(() => import("./screens-grids").then(module => ({ default: module.GridScreen })));
const GridsScreen = lazy(() => import("./screens-grids").then(module => ({ default: module.GridsScreen })));
const DashboardScreen = lazy(() => import("./screens-wireframe").then(module => ({ default: module.DashboardScreen })));
const SampleViewerScreen = lazy(() => import("./screens-wireframe").then(module => ({ default: module.SampleViewerScreen })));
const TransfersScreen = lazy(() => import("./screens-wireframe").then(module => ({ default: module.TransfersScreen })));
const GenerationQueueScreen = lazy(() => import("./generator").then(module => ({ default: module.GenerationQueueScreen })));
const ImageGeneratorScreen = lazy(() => import("./generator").then(module => ({ default: module.ImageGeneratorScreen })));
const TrainingLaunchCreateScreen = lazy(() => import("./training").then(module => ({ default: module.TrainingLaunchCreateScreen })));
const TrainingLaunchScreen = lazy(() => import("./training").then(module => ({ default: module.TrainingLaunchScreen })));
const ReadinessScreen = lazy(() => import("./readiness").then(module => ({ default: module.ReadinessScreen })));
const DocsScreen = lazy(() => import("./docs-screen").then(module => ({ default: module.DocsScreen })));
type AccessState = "checking" | "locked" | "ready" | "error";
type RouteGate = "ready" | "resolving" | "error";


function RemoteAccessPrompt({ error, onAuthenticated }: { error?: string; onAuthenticated: () => void }) {
  if (error) {
    return <div className="vela-theme"><main className="vela-main"><Panel title="Modelfiche unavailable">
      <div className="vela-notice vela-notice-error" role="alert">{error}</div>
      <button className="vela-button" type="button" onClick={() => location.reload()}>Retry connection</button>
    </Panel></main></div>;
  }
  return <div className="vela-theme"><main className="vela-main"><Panel title="Remote access">
    <p>This Modelfiche host requires its Cloudflare service token before remote commands are enabled.</p>
    <Form submit="Connect securely" onSubmit={async form => {
      await establishRemoteSession(String(form.get("client_id") ?? ""), String(form.get("client_secret") ?? ""));
      onAuthenticated();
    }}>
      <div className="form-grid">
        <Field label="Client ID"><input name="client_id" autoComplete="username" required autoFocus /></Field>
        <Field label="Client secret"><input name="client_secret" type="password" autoComplete="current-password" required /></Field>
      </div>
    </Form>
  </Panel></main></div>;
}



export function App() {
  const [accessState, setAccessState] = useState<AccessState>("checking");
  const [accessError, setAccessError] = useState("");
  useEffect(() => {
    let active = true;
    void api("/api/health").then(() => {
      if (active) setAccessState("ready");
    }).catch(reason => {
      if (!active) return;
      if (reason instanceof ApiError && reason.status === 401) {
        setAccessState("locked");
        return;
      }
      setAccessError(reason instanceof Error ? reason.message : String(reason));
      setAccessState("error");
    });
    return () => { active = false; };
  }, []);
  const rawRoute = useHashRoute();
  const parsedRoute = parseWorkspaceHash(window.location.hash);
  const [routeGate, setRouteGate] = useState<RouteGate>(parsedRoute.legacy ? "resolving" : "ready");
  const [routeError, setRouteError] = useState("");
  useCanonicalWorkspaceLinks(parsedRoute.workspaceSlug);
  useEffect(() => {
    if (accessState !== "ready" || !parsedRoute.legacy) {
      setRouteGate("ready");
      return;
    }
    let active = true;
    setRouteGate("resolving");
    const resolveWorkspace = async () => {
      const reference = legacyEntityReference(parsedRoute.route);
      if (reference) {
        const resolution = await apiUnscoped<{ workspace_id: string; workspace_slug: string }>(
          `/api/workspace-resolutions/${encodeURIComponent(reference.entityType)}/${encodeURIComponent(reference.entityId)}`,
        );
        return { id: resolution.workspace_id, slug: resolution.workspace_slug };
      }
      const workspaces = await apiUnscoped<Array<{ id: string; slug: string }>>("/api/workspaces");
      const preferred = activeWorkspaceId();
      return workspaces.find(workspace => workspace.id === preferred) ?? workspaces[0] ?? null;
    };
    void resolveWorkspace().then(workspace => {
      if (!active) return;
      if (!workspace) throw new Error("No workspace is available");
      setActiveWorkspaceId(workspace.id);
      const canonical = canonicalWorkspaceHref(`#/${parsedRoute.route}`, workspace.slug);
      history.replaceState(history.state, "", canonical);
      window.dispatchEvent(new HashChangeEvent("hashchange"));
      setRouteGate("ready");
    }).catch(reason => {
      if (!active) return;
      setRouteError(reason instanceof Error ? reason.message : String(reason));
      setRouteGate("error");
    });
    return () => { active = false; };
  }, [accessState, rawRoute, parsedRoute.legacy, parsedRoute.route]);
  const [path, rawQuery = ""] = rawRoute.split("?");
  const parts = path.split("/").filter(Boolean);
  const section = parts[0] || "dashboard";
  const id = parts[1] || "";
  const params = new URLSearchParams(rawQuery);
  const routedSection = section === "evals" ? "grids" : section;
  const { theme, toggleTheme } = useThemeController();
  if (accessState === "checking") {
    return <div className="vela-theme"><main className="vela-main" aria-busy="true">Checking access…</main></div>;
  }
  if (accessState !== "ready") {
    return <RemoteAccessPrompt error={accessState === "error" ? accessError : undefined} onAuthenticated={() => setAccessState("ready")} />;
  }
  if (routeGate === "resolving") {
    return <div className="vela-theme"><main className="vela-main" aria-busy="true">Resolving workspace…</main></div>;
  }
  if (routeGate === "error") {
    return <div className="vela-theme"><main className="vela-main"><Panel title="Workspace unavailable"><div className="vela-notice vela-notice-error" role="alert">{routeError}</div></Panel></main></div>;
  }
  let content;
  if (routedSection === "image") {
    const galleryParams = new URLSearchParams(params);
    galleryParams.set("asset", id);
    content = <GalleryScreen projectId={params.get("project") ?? ""} params={galleryParams} />;
  }
  else if (routedSection === "samples") content = <SampleViewerScreen runId={id} initialStep={params.get("step") ?? ""} />;
  else if (routedSection === "eval") content = <EvalScreen id={id} projectId={params.get("project") ?? ""} />;
  else if (routedSection === "eval-comparison") content = <EvalComparisonScreen id={id} />;
  else if (routedSection === "grid") content = <GridScreen id={id} projectId={params.get("project") ?? ""} />;
  else content = <WorkbenchShell section={routedSection} id={id} params={params} theme={theme} onToggleTheme={toggleTheme}>{renderScreen(routedSection, id, params)}</WorkbenchShell>;
  return <div className="vela-theme"><Suspense fallback={<main className="vela-main" aria-busy="true">Loading view…</main>}>{content}</Suspense></div>;
}

function renderScreen(section: string, id: string, params: URLSearchParams) {
  const projectId = params.get("project") ?? "";
  switch (section) {
    case "dashboard": return <DashboardScreen createProject={params.get("create") === "project"} />;
    case "projects": return <ProjectsScreen />;
    case "project": return <ProjectScreen id={id} editing={params.get("edit") === "1"} />;
    case "import": return <ImportScreen initialProject={projectId} source={params.get("source")} />;
    case "jobs": return <JobScreen id={id} general={params.get("job") === "1"} />;
    case "datasets": return <DatasetsScreen projectId={projectId} params={params} />;
    case "dataset": return <DatasetScreen id={id} />;
    case "runs": return <RunsScreen projectId={projectId} params={params} />;
    case "run": return <RunScreen id={id} />;
    case "training-new": return <TrainingLaunchCreateScreen params={params} />;
    case "training": return <TrainingLaunchScreen id={id} />;
    case "checkpoint": return <CheckpointScreen id={id} />;
    case "gallery": return <GalleryScreen projectId={projectId} params={params} />;
    case "generate": return <ImageGeneratorScreen params={params} />;
    case "generation-queue": return <GenerationQueueScreen />;
    case "models": return <ModelsScreen projectId={projectId} params={params} />;
    case "model": return <ModelScreen id={id} />;
    case "model-version": return <ModelVersionScreen id={id} />;
    case "eval": return <EvalScreen id={id} projectId={projectId} />;
    case "eval-comparison": return <EvalComparisonScreen id={id} />;
    case "grids": return <GridsScreen params={params} />;
    case "grid": return <GridScreen id={id} projectId={projectId} />;
    case "reviews": return <ReviewsScreen projectId={projectId} subject={params.get("subject") ?? ""} />;
    case "transfers": return <TransfersScreen mode={params.get("mode") ?? ""} params={params} />;
    case "docs": return <DocsScreen />;
    case "readiness": return <ReadinessScreen />;
    case "settings": return <SettingsScreen />;
    default: return <main><h1>Not found</h1><a href={`#/projects${routeQuery({})}`}>Return to projects</a></main>;
  }
}
