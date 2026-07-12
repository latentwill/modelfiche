import { useHashRoute } from "./hooks";
import { ImportScreen, JobScreen, ProjectScreen, ProjectsScreen, SettingsScreen } from "./screens-core";
import { CheckpointScreen, DatasetScreen, DatasetsScreen, GalleryScreen, ModelScreen, ModelsScreen, RunScreen, RunsScreen } from "./screens-assets";
import { EvalScreen, EvalsScreen, GridScreen, GridsScreen, PromptSetsScreen, ReviewsScreen } from "./screens-evals";
import { WorkbenchShell } from "./shell";
import { DashboardScreen, ImageReviewScreen, SampleViewerScreen, TransfersScreen } from "./screens-wireframe";
import { GenerationQueueScreen, ImageGeneratorScreen } from "./generator";

export function App() {
  const rawRoute = useHashRoute();
  const [path, rawQuery = ""] = rawRoute.split("?");
  const parts = path.split("/").filter(Boolean);
  const section = parts[0] || "dashboard";
  const id = parts[1] || "";
  const params = new URLSearchParams(rawQuery);
  let content;
  if (section === "image") content = <ImageReviewScreen id={id} />;
  else if (section === "samples") content = <SampleViewerScreen runId={id} initialStep={params.get("step") ?? ""} />;
  else if (section === "eval") content = <EvalScreen id={id} />;
  else if (section === "grid") content = <GridScreen id={id} />;
  else content = <WorkbenchShell section={section} id={id} params={params}>{renderScreen(section, id, params)}</WorkbenchShell>;
  return <div className="vela-theme">{content}</div>;
}

function renderScreen(section: string, id: string, params: URLSearchParams) {
  const projectId = params.get("project") ?? "";
  switch (section) {
    case "dashboard": return <DashboardScreen createProject={params.get("create") === "project"} />;
    case "projects": return <ProjectsScreen />;
    case "project": return <ProjectScreen id={id} editing={params.get("edit") === "1"} />;
    case "import": return <ImportScreen initialProject={projectId} />;
    case "jobs": return <JobScreen id={id} general={params.get("job") === "1"} />;
    case "datasets": return <DatasetsScreen projectId={projectId} />;
    case "dataset": return <DatasetScreen id={id} />;
    case "runs": return <RunsScreen projectId={projectId} />;
    case "run": return <RunScreen id={id} />;
    case "checkpoint": return <CheckpointScreen id={id} />;
    case "gallery": return <GalleryScreen projectId={projectId} params={params} />;
    case "generate": return <ImageGeneratorScreen params={params} />;
    case "generation-queue": return <GenerationQueueScreen />;
    case "models": return <ModelsScreen projectId={projectId} />;
    case "model": return <ModelScreen id={id} />;
    case "prompt-sets": return <PromptSetsScreen projectId={projectId} promptSetId={id} />;
    case "evals": return <EvalsScreen projectId={projectId} initialModelVersionId={params.get("model_version") ?? ""} />;
    case "eval": return <EvalScreen id={id} />;
    case "grids": return <GridsScreen projectId={projectId} initialModelVersionId={params.get("model_version") ?? ""} />;
    case "grid": return <GridScreen id={id} />;
    case "reviews": return <ReviewsScreen projectId={projectId} subject={params.get("subject") ?? ""} />;
    case "transfers": return <TransfersScreen mode={params.get("mode") ?? ""} params={params} />;
    case "settings": return <SettingsScreen />;
    default: return <main><h1>Not found</h1><a href="#/projects">Return to projects</a></main>;
  }
}
