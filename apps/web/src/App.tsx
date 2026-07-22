import { lazy, Suspense } from "react";
import { useHashRoute } from "./hooks";
import { WorkbenchShell, useThemeController } from "./shell";

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
const EvalsScreen = lazy(() => import("./screens-evals").then(module => ({ default: module.EvalsScreen })));
const ReviewsScreen = lazy(() => import("./screens-evals").then(module => ({ default: module.ReviewsScreen })));
const GridScreen = lazy(() => import("./screens-grids").then(module => ({ default: module.GridScreen })));
const GridsScreen = lazy(() => import("./screens-grids").then(module => ({ default: module.GridsScreen })));
const DashboardScreen = lazy(() => import("./screens-wireframe").then(module => ({ default: module.DashboardScreen })));
const ImageReviewScreen = lazy(() => import("./screens-wireframe").then(module => ({ default: module.ImageReviewScreen })));
const SampleViewerScreen = lazy(() => import("./screens-wireframe").then(module => ({ default: module.SampleViewerScreen })));
const TransfersScreen = lazy(() => import("./screens-wireframe").then(module => ({ default: module.TransfersScreen })));
const GenerationQueueScreen = lazy(() => import("./generator").then(module => ({ default: module.GenerationQueueScreen })));
const ImageGeneratorScreen = lazy(() => import("./generator").then(module => ({ default: module.ImageGeneratorScreen })));
const TrainingLaunchCreateScreen = lazy(() => import("./training").then(module => ({ default: module.TrainingLaunchCreateScreen })));
const TrainingLaunchScreen = lazy(() => import("./training").then(module => ({ default: module.TrainingLaunchScreen })));
const ReadinessScreen = lazy(() => import("./readiness").then(module => ({ default: module.ReadinessScreen })));
const DocsScreen = lazy(() => import("./docs-screen").then(module => ({ default: module.DocsScreen })));

export function App() {
  const rawRoute = useHashRoute();
  const [path, rawQuery = ""] = rawRoute.split("?");
  const parts = path.split("/").filter(Boolean);
  const section = parts[0] || "dashboard";
  const id = parts[1] || "";
  const params = new URLSearchParams(rawQuery);
  const { theme, toggleTheme } = useThemeController();
  let content;
  if (section === "image") content = <ImageReviewScreen id={id} />;
  else if (section === "samples") content = <SampleViewerScreen runId={id} initialStep={params.get("step") ?? ""} />;
  else if (section === "eval") content = <EvalScreen id={id} />;
  else if (section === "eval-comparison") content = <EvalComparisonScreen id={id} />;
  else if (section === "grid") content = <GridScreen id={id} projectId={params.get("project") ?? ""} />;
  else content = <WorkbenchShell section={section} id={id} params={params} theme={theme} onToggleTheme={toggleTheme}>{renderScreen(section, id, params)}</WorkbenchShell>;
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
    case "datasets": return <DatasetsScreen projectId={projectId} />;
    case "dataset": return <DatasetScreen id={id} />;
    case "runs": return <RunsScreen projectId={projectId} />;
    case "run": return <RunScreen id={id} />;
    case "training-new": return <TrainingLaunchCreateScreen params={params} />;
    case "training": return <TrainingLaunchScreen id={id} />;
    case "checkpoint": return <CheckpointScreen id={id} />;
    case "gallery": return <GalleryScreen projectId={projectId} params={params} />;
    case "generate": return <ImageGeneratorScreen params={params} />;
    case "generation-queue": return <GenerationQueueScreen />;
    case "models": return <ModelsScreen projectId={projectId} />;
    case "model": return <ModelScreen id={id} />;
    case "model-version": return <ModelVersionScreen id={id} />;
    case "evals": return <EvalsScreen projectId={projectId} initialModelVersionId={params.get("model_version") ?? ""} />;
    case "eval": return <EvalScreen id={id} />;
    case "eval-comparison": return <EvalComparisonScreen id={id} />;
    case "grids": return <GridsScreen params={params} />;
    case "grid": return <GridScreen id={id} projectId={projectId} />;
    case "reviews": return <ReviewsScreen projectId={projectId} subject={params.get("subject") ?? ""} />;
    case "transfers": return <TransfersScreen mode={params.get("mode") ?? ""} params={params} />;
    case "docs": return <DocsScreen />;
    case "readiness": return <ReadinessScreen />;
    case "settings": return <SettingsScreen />;
    default: return <main><h1>Not found</h1><a href="#/projects">Return to projects</a></main>;
  }
}
