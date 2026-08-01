import { idOf, listOf, query, routeQuery, str } from "./api";
import { useResource } from "./hooks";
import { Notice, Panel, Status } from "./ui";

type Row = Record<string, unknown>;
type ActiveRunResources = {
  runs: Row[];
  loading: boolean;
  error: string;
};

export const ACTIVE_RUN_LIMIT = 6;
const ACTIVE_STATUSES = ["running"] as const;
const rows = (value: unknown) => listOf<Row>(value);

function runTimestamp(run: Row) {
  const parsed = Date.parse(str(run.last_event_at ?? run.updated_at ?? run.started_at ?? run.created_at, ""));
  return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed;
}

export function orderActiveRuns(runs: Row[]) {
  const unique = new Map<string, Row>();
  runs.forEach(run => {
    const id = idOf(run);
    if (id && ACTIVE_STATUSES.includes(str(run.status, "").toLowerCase() as (typeof ACTIVE_STATUSES)[number])) unique.set(id, run);
  });
  return [...unique.values()].sort((left, right) => {
    const statusDifference = ACTIVE_STATUSES.indexOf(str(left.status).toLowerCase() as (typeof ACTIVE_STATUSES)[number])
      - ACTIVE_STATUSES.indexOf(str(right.status).toLowerCase() as (typeof ACTIVE_STATUSES)[number]);
    return statusDifference || runTimestamp(right) - runTimestamp(left) || idOf(left).localeCompare(idOf(right));
  });
}

export function useActiveRuns(projectId = "", limit = ACTIVE_RUN_LIMIT): ActiveRunResources {
  const running = useResource<unknown>(`/api/runs${query({ project_id: projectId || undefined, status: "running", limit })}`);
  return {
    runs: orderActiveRuns(rows(running.data)).slice(0, limit),
    loading: running.loading,
    error: running.error,
  };
}

function metricValue(metric: unknown, fallback = "Unavailable") {
  if (!metric || typeof metric !== "object" || Array.isArray(metric)) return str(metric, fallback);
  const value = metric as Row;
  return str(value.value_text ?? value.value, fallback);
}

function uploadSummary(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return str(value, "none");
  const entries = Object.entries(value as Row)
    .filter(([, count]) => Number.isFinite(Number(count)) && Number(count) > 0)
    .map(([state, count]) => `${count} ${state.replaceAll("_", " ")}`);
  return entries.length ? entries.join(" · ") : "none";
}

function formatElapsed(value: unknown) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "Elapsed unavailable";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = Math.floor(seconds % 60);
  return `Elapsed ${hours ? `${hours}h ` : ""}${minutes ? `${minutes}m ` : ""}${remainder}s`;
}

export function ActiveTrainingPanel({ projectId = "" }: { projectId?: string }) {
  const resource = useActiveRuns(projectId);
  if (!resource.loading && !resource.error && !resource.runs.length) return null;
  return <Panel title="Active training" className="active-training-panel">
    <Notice error={resource.error} loading={resource.loading && !resource.runs.length} />
    {!!resource.runs.length && <div className="active-training-list" aria-label="Active training runs">
      {resource.runs.map(run => <ActiveTrainingLink key={idOf(run)} run={run} projectId={projectId} />)}
    </div>}
  </Panel>;
}

export function ActiveTrainingNavigation({ projectId }: { projectId: string }) {
  const resource = useActiveRuns(projectId, 4);
  if (!resource.runs.length) return null;
  return <div className="vela-tree-group active-training-navigation">
    <a className="vela-nav-group" href={`#/runs${routeQuery({ project: projectId })}`}>Active training</a>
    {resource.runs.map(run => <a className="vela-nav-item" key={idOf(run)} href={`#/run/${idOf(run)}${routeQuery({ project: projectId })}`} aria-label={`${str(run.name, "Training run")}, status ${str(run.status, "unknown")}`}>
      <span className="vela-nav-text">{str(run.name, "Training run")}</span>
      <span className="active-training-indicator" aria-hidden="true">{str(run.status).toLowerCase() === "running" ? "LIVE" : "WAIT"}</span>
    </a>)}
  </div>;
}

function ActiveTrainingLink({ run, projectId = "" }: { run: Row; projectId?: string }) {
  const latestLoss = run.latest_loss && typeof run.latest_loss === "object" ? run.latest_loss as Row : null;
  const lossName = str(latestLoss?.name, "Loss");
  return <a className="active-training-card" href={`#/run/${idOf(run)}${routeQuery({ project: projectId || undefined })}`} aria-label={`Open ${str(run.name, "training run")}, status ${str(run.status, "unknown")}`}>
    <span className="active-training-heading"><strong>{str(run.name, "Training run")}</strong><Status value={run.status} /></span>
    <span className="active-training-metrics">
      <span><small>Step</small><strong>{str(run.current_step, "—")}</strong></span>
      <span><small>{lossName}</small><strong>{metricValue(latestLoss)}</strong></span>
      <span><small>Learning rate</small><strong>{metricValue(run.learning_rate, "—")}</strong></span>
    </span>
    <span className="active-training-footer"><span>{formatElapsed(run.elapsed_seconds)}</span><span>Uploads: {uploadSummary(run.upload_status)}</span></span>
  </a>;
}
