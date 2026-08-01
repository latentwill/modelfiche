import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Download, ExternalLink, Grid3X3, ZoomIn, ZoomOut } from "lucide-react";
import { api, idOf, jsonBody, listOf, routeQuery, str } from "./api";
import { AssetImage, resolveAssetDelivery } from "./asset-image";
import { useResource } from "./hooks";
import { Dropdown, Field, Notice, Page, Panel, Status } from "./ui";
import { connectedFalGenerationModels, type GenerationModel } from "./generation";
import { EndpointFields, parametersFromForm } from "./generator";
import {
  buildExperimentPlanPayload,
  buildExperimentCreatePayload,
  buildExperimentQueuePayload,
  validateExperimentPlan,
  type ExperimentCase,
  type ExperimentPlan,
  type ExperimentTarget,
  type JsonObject,
  type PlanAxis,
} from "./experiment-plan";
import { parseGridAxisValues, type GridAxisSchema } from "./grid";
import { downloadGridImage, type GridExportSize, type GridExportSlice } from "./grid-export";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);
const CONTRACT_VERSION = "2026-07-22.v1" as const;
const axis = (name: string, values: unknown[]): PlanAxis => ({ name, values: values.map(value => ({ value })) });

function caseFromPrompt(prompt: string, ordinal: number): ExperimentCase {
  return { case_id: `case-${ordinal}`, ordinal, input: { prompt } };
}

function targetFromModel(model: GenerationModel, schema: Row, ordinal: number): ExperimentTarget {
  const checkpointStep = Number(model.checkpointLabel.match(/\bstep\s+(\d+)\b/i)?.[1]);
  return {
    target_id: `target-${model.id}`,
    ordinal,
    provider: model.provider ?? "fal",
    endpoint_id: model.endpoint ?? "",
    model_version_id: model.id,
    checkpoint_revision_id: model.checkpointRevisionId ?? null,
    display_name: `${model.name} · ${model.checkpointLabel}`,
    checkpoint_step: Number.isFinite(checkpointStep) ? checkpointStep : undefined,
    schema_digest: str(schema.schema_digest ?? schema.digest, ""),
    schema,
    fixed_target: { provider: model.provider ?? "fal", endpoint_id: model.endpoint ?? "", model_version_id: model.id, checkpoint_revision_id: model.checkpointRevisionId ?? null },
    shared_overrides: {},
    target_overrides: {},
  };
}

function targetFromEndpoint(endpoint: Row, ordinal: number): ExperimentTarget {
  const endpointId = str(endpoint.endpoint_id ?? endpoint.id, "");
  return {
    target_id: `target-endpoint-${endpointId}`,
    ordinal,
    provider: "fal",
    endpoint_id: endpointId,
    model_version_id: null,
    checkpoint_revision_id: null,
    schema_digest: str(endpoint.schema_digest ?? endpoint.digest, ""),
    schema: endpoint,
    fixed_target: { provider: "fal", endpoint_id: endpointId, model_version_id: null, checkpoint_revision_id: null },
    shared_overrides: {},
    target_overrides: {},
  };
}

function fieldDefinition(target: ExperimentTarget, field: string): Row | undefined {
  const grid = target.schema.grid && typeof target.schema.grid === "object" ? target.schema.grid as Row : {};
  const axisFields = grid.axis_fields && typeof grid.axis_fields === "object" ? grid.axis_fields as Row : {};
  const fields = target.schema.fields && typeof target.schema.fields === "object" ? target.schema.fields as Row : {};
  const fixedFields = grid.fixed_field_schema && typeof grid.fixed_field_schema === "object" ? grid.fixed_field_schema as Row : {};
  const value = fields[field] ?? axisFields[field] ?? fixedFields[field];
  return value && typeof value === "object" ? value as Row : undefined;
}

/** Return the values safe for every selected endpoint, or undefined when incompatible. */
export function intersectFieldSchemas(definitions: Row[]): GridAxisSchema | undefined {
  if (!definitions.length) return undefined;
  const first = definitions[0];
  const type = str(first.type ?? first.parser, "string");
  const parser = str(first.parser ?? first.type, type);
  if (definitions.some(definition => str(definition.type ?? definition.parser, "string") !== type || str(definition.parser ?? definition.type, type) !== parser)) return undefined;
  const merged: Row = { ...first, type, parser };
  const optionDefinitions = definitions.map(definition => Array.isArray(definition.options) ? definition.options : null);
  if (optionDefinitions.some(options => options !== null)) {
    if (optionDefinitions.some(options => options === null)) return undefined;
    const common = (optionDefinitions[0] ?? []).filter(value => optionDefinitions.every(options => (options ?? []).some(candidate => JSON.stringify(candidate) === JSON.stringify(value))));
    if (!common.length) return undefined;
    merged.options = common;
  }
  const minima = definitions.map(definition => Number(definition.min)).filter(Number.isFinite);
  const maxima = definitions.map(definition => Number(definition.max)).filter(Number.isFinite);
  if (minima.length) merged.min = Math.max(...minima);
  if (maxima.length) merged.max = Math.min(...maxima);
  if (merged.min !== undefined && merged.max !== undefined && Number(merged.min) > Number(merged.max)) return undefined;
  const steps = definitions.map(definition => Number(definition.step)).filter(Number.isFinite);
  if (steps.length) merged.step = Math.max(...steps);
  const customs = definitions.map(definition => definition.custom && typeof definition.custom === "object" ? definition.custom as Row : null);
  if (customs.some(custom => custom !== null)) {
    if (customs.some(custom => custom === null)) return undefined;
    const customMin = customs.map(custom => Number(custom?.min)).filter(Number.isFinite);
    const customMax = customs.map(custom => Number(custom?.max)).filter(Number.isFinite);
    const customSteps = customs.map(custom => Number(custom?.step)).filter(Number.isFinite);
    merged.custom = {
      ...(customMin.length ? { min: Math.max(...customMin) } : {}),
      ...(customMax.length ? { max: Math.min(...customMax) } : {}),
      ...(customSteps.length ? { step: Math.max(...customSteps) } : {}),
    };
    const custom = merged.custom as Row;
    if (custom.min !== undefined && custom.max !== undefined && Number(custom.min) > Number(custom.max)) return undefined;
  }
  return merged as GridAxisSchema;
}

function schemaFor(targets: ExperimentTarget[], field: string): GridAxisSchema | undefined {
  const definitions = targets.map(target => fieldDefinition(target, field));
  if (definitions.some(definition => !definition)) return undefined;
  return intersectFieldSchemas(definitions as Row[]);
}

function targetKey(target: ExperimentTarget): string {
  return target.model_version_id ? String(target.model_version_id) : `endpoint:${target.endpoint_id}`;
}

function axisContext(name: string, kind: "target" | "case") {
  return kind === "target" ? name === "target" : name === "case" || name === "prompt";
}

function axisValuesFor(name: string, text: string, targets: ExperimentTarget[], cases: ExperimentCase[], targetKeys: string[], schema?: GridAxisSchema) {
  if (name === "target") {
    return targets.filter(target => targetKeys.includes(targetKey(target))).sort((left, right) => (left.checkpoint_step ?? Number.MAX_SAFE_INTEGER) - (right.checkpoint_step ?? Number.MAX_SAFE_INTEGER)).map(target => ({ value: target.target_id, label: target.display_name ?? `${target.endpoint_id}${target.model_version_id ? ` · ${target.model_version_id}` : " · base endpoint"}` }));
  }
  if (name === "case" || name === "prompt") {
    return cases.map(item => ({ value: item.case_id, label: str(item.input.prompt ?? item.input.text, item.case_id) }));
  }
  if (!schema) return text.split(/\r?\n/).map(value => value.trim()).filter(Boolean).map(value => ({ value, label: value }));
  try {
    return parseGridAxisValues(text, schema).map(entry => ({ value: entry.value, label: entry.label }));
  } catch {
    return [];
  }
}

function axisHas(plan: ExperimentPlan, kind: "target" | "case"): boolean {
  return [plan.axes.x, plan.axes.y, plan.axes.z].some(axisValue => axisValue && axisContext(axisValue.name.toLowerCase(), kind));
}

function TargetPicker({ targets, selected, axisActive, onChange }: { targets: ExperimentTarget[]; selected: string[]; axisActive: boolean; onChange: (values: string[]) => void }) {
  const groups = new Map<string, ExperimentTarget[]>();
  for (const target of [...targets].sort((left, right) => (left.checkpoint_step ?? Number.MAX_SAFE_INTEGER) - (right.checkpoint_step ?? Number.MAX_SAFE_INTEGER))) groups.set(target.endpoint_id, [...(groups.get(target.endpoint_id) ?? []), target]);
  return <Field label={axisActive ? "Target axis selections" : "Fixed target"} hint={axisActive ? "Every selected target is represented exactly once in the Target axis." : "The fixed target is context, not a comparison dimension."}>
    <select className="vela-multi-select" aria-label={axisActive ? "Target axis targets" : "Fixed target"} multiple={axisActive} size={axisActive ? Math.min(8, Math.max(3, targets.length)) : undefined} value={selected} onChange={event => onChange(axisActive ? Array.from(event.target.selectedOptions, option => option.value) : event.target.value ? [event.target.value] : [])}>
      {Array.from(groups.entries()).map(([endpointId, endpointTargets]) => <optgroup label={endpointId} key={endpointId}>{endpointTargets.map(target => <option key={targetKey(target)} value={targetKey(target)}>{target.model_version_id ? target.display_name ?? `Registered LoRA · model ${target.model_version_id}` : "Base endpoint · no checkpoint"}</option>)}</optgroup>)}
    </select>
  </Field>;
}

export function GridsScreen({ params }: { params: URLSearchParams }) {
  const initialModel = params.get("model_version") ?? "";
  const [project, setProject] = useState(params.get("project") ?? "");
  const [targetKeys, setTargetKeys] = useState<string[]>(initialModel ? [initialModel] : []);
  const [xField, setXField] = useState("case");
  const [yField, setYField] = useState("target");
  const [zField, setZField] = useState("");
  const [xText, setXText] = useState("A portrait\nA landscape");
  const [yText, setYText] = useState("");
  const [zText, setZText] = useState("");
  const [fixedPrompt, setFixedPrompt] = useState("");
  const [shared, setShared] = useState<JsonObject>({});
  const [overrides, setOverrides] = useState<Record<string, JsonObject>>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [queuedGridId, setQueuedGridId] = useState("");
  const attemptKeys = useRef({ create: crypto.randomUUID(), queue: crypto.randomUUID() });
  const projects = useResource<unknown>("/api/projects?limit=100");
  const versions = useResource<unknown>(project ? `/api/model-versions?project_id=${encodeURIComponent(project)}` : null);
  const endpointRows = useResource<unknown>("/api/eval-endpoints");
  const grids = useResource<unknown>(project ? `/api/projects/${encodeURIComponent(project)}/grids` : null);
  const models = useMemo(() => connectedFalGenerationModels(rows(versions.data), project), [versions.data, project]);
  const endpointMap = useMemo(() => new Map(rows(endpointRows.data).map(row => [str(row.endpoint_id ?? row.id), row])), [endpointRows.data]);
  const targets = useMemo(() => targetKeys.map(key => key.startsWith("endpoint:") ? targetFromEndpoint(endpointMap.get(key.slice("endpoint:".length)) ?? {}, 0) : (() => { const model = models.find(item => item.id === key); return model ? targetFromModel(model, endpointMap.get(str(model.endpoint)) ?? {}, 0) : null; })()).filter((item): item is ExperimentTarget => Boolean(item)).map((target, ordinal) => ({ ...target, ordinal })), [targetKeys, models, endpointMap]);
  const allTargetOptions = useMemo(() => [
    ...models.map(model => ({ key: model.id, target: targetFromModel(model, endpointMap.get(str(model.endpoint)) ?? {}, 0) })),
    ...rows(endpointRows.data).map((row, ordinal) => ({ key: `endpoint:${str(row.endpoint_id ?? row.id)}`, target: targetFromEndpoint(row, ordinal) })),
  ], [models, endpointRows.data, endpointMap]);
  const availableTargets = allTargetOptions.map(item => item.target);
  const axisFields = useMemo(() => {
    const common = new Set<string>();
    targets.forEach((target, index) => {
      const grid = target.schema.grid && typeof target.schema.grid === "object" ? target.schema.grid as Row : {};
      const axisDefinitions = grid.axis_fields && typeof grid.axis_fields === "object" ? grid.axis_fields as Row : {};
      const fields = Object.keys(axisDefinitions);
      if (!index) fields.forEach(field => common.add(field));
      else Array.from(common).filter(field => !fields.includes(field)).forEach(field => common.delete(field));
    });
    return Array.from(new Set(["case", "prompt", "target", "seed", ...Array.from(common).filter(field => field !== "model" && field !== "checkpoint_step")]));
  }, [targets]);
  const commonFields = useMemo(() => {
    if (!targets.length) return {};
    const firstSchema = targets[0].schema;
    const fieldNames = Object.keys(((firstSchema.fields && typeof firstSchema.fields === "object") ? firstSchema.fields : {}) as Row);
    const omit = new Set([xField, yField, zField, "prompt", "model", "checkpoint_step", "sync_mode", "loras", "endpoint_id", "model_version_id", "checkpoint_revision_id"]);
    const fields: Row = {};
    for (const field of fieldNames) {
      if (!omit.has(field)) {
        const definition = schemaFor(targets, field);
        if (definition) fields[field] = definition;
      }
    }
    return fields;
  }, [targets, xField, yField, zField]);
  const commonSchema = useMemo<Row>(() => ({ fields: commonFields, request_fields: Object.keys(commonFields), defaults: Object.fromEntries(Object.keys(commonFields).map(field => [field, fieldDefinition(targets[0], field)?.default])) }), [commonFields, targets]);
  const activeCaseAxis = [xField, yField, zField].some(name => name === "case" || name === "prompt");
  const caseText = xField === "case" || xField === "prompt" ? xText : yField === "case" || yField === "prompt" ? yText : zText;
  const casePrompts = activeCaseAxis ? caseText.split(/\r?\n/).map(value => value.trim()).filter(Boolean) : [fixedPrompt.trim()];
  const cases = useMemo(() => casePrompts.map((prompt, ordinal) => caseFromPrompt(prompt, ordinal)), [casePrompts]);
  const xSchema = schemaFor(targets, xField);
  const ySchema = schemaFor(targets, yField);
  const zSchema = zField ? schemaFor(targets, zField) : undefined;
  const xValues = axisValuesFor(xField, xText, availableTargets, cases, targetKeys, xSchema);
  const yValues = axisValuesFor(yField, yText, availableTargets, cases, targetKeys, ySchema);
  const zValues = zField ? axisValuesFor(zField, zText, availableTargets, cases, targetKeys, zSchema) : [];
  const plan = useMemo<ExperimentPlan>(() => {
    const xAxis = axis(xField, xValues.map(item => item.value));
    const yAxis = axis(yField, yValues.map(item => item.value));
    const zAxis = zField ? axis(zField, zValues.map(item => item.value)) : null;
    return {
      contract_version: CONTRACT_VERSION,
      project_id: project,
      axes: { x: xAxis, y: yAxis, z: zAxis },
      cases,
      targets: targets.map(target => ({ ...target, target_overrides: overrides[target.target_id] ?? {} })),
      fixed_case_id: activeCaseAxis ? undefined : cases[0]?.case_id,
      fixed_target_id: axisHas({ axes: { x: xAxis, y: yAxis, z: zAxis }, cases, targets, shared_params: shared, endpoint_defaults: {}, contract_version: CONTRACT_VERSION, project_id: project }, "target") ? undefined : targets[0]?.target_id,
      shared_params: shared,
      endpoint_defaults: {},
    };
  }, [project, xField, yField, zField, xValues, yValues, zValues, cases, targets, overrides, activeCaseAxis, shared]);
  const planErrors = validateExperimentPlan(plan);
  const parseError = [xField !== "target" && (xText.trim() || xField === "case" || xField === "prompt") && !xValues.length ? `Enter valid X ${xField} values.` : "", yField !== "target" && (yText.trim() || yField === "case" || yField === "prompt") && !yValues.length ? `Enter valid Y ${yField} values.` : "", zField && zField !== "target" && (zText.trim() || zField === "case" || zField === "prompt") && !zValues.length ? `Enter valid Z ${zField} values.` : ""].find(Boolean) ?? "";
  const configError = !project ? "Select a project." : !targets.length ? "Select a registered target or FAL base endpoint." : parseError || planErrors[0] || (!xValues.length || !yValues.length ? "Add values to both axes." : "");
  const error = configError;
  const planSignature = JSON.stringify(buildExperimentPlanPayload(plan));
  const previousPlanSignature = useRef(planSignature);
  useEffect(() => {
    if (previousPlanSignature.current !== planSignature) {
      previousPlanSignature.current = planSignature;
      attemptKeys.current = { create: crypto.randomUUID(), queue: crypto.randomUUID() };
      setQueuedGridId("");
      setMessage("");
    }
  }, [planSignature]);
  const resetProject = (value: string) => {
    setProject(value);
    setTargetKeys([]);
    setShared({});
    setOverrides({});
    setQueuedGridId("");
  };
  async function generate() {
    if (error || busy) return;
    setBusy(true); setMessage("");
    try {
      const saved = await api<Row>(`/api/projects/${encodeURIComponent(project)}/grids`, jsonBody(buildExperimentCreatePayload("Grid experiment", attemptKeys.current.create, plan)));
      const gridId = idOf(saved);
      const digest = str(saved.plan_digest ?? saved.digest, "");
      const queued = await api<Row>(`/api/projects/${encodeURIComponent(project)}/grids/${encodeURIComponent(gridId)}/queue`, jsonBody(buildExperimentQueuePayload(attemptKeys.current.queue, saved.plan_version, digest, rows(saved.cells))));
      const resultId = idOf(queued) || gridId;
      setQueuedGridId(gridId);
      setMessage(`Grid queued as ${resultId}.`);
      attemptKeys.current = { create: crypto.randomUUID(), queue: crypto.randomUUID() };
      await grids.reload();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : String(reason));
    } finally { setBusy(false); }
  }
  const loading = projects.loading || versions.loading || endpointRows.loading || grids.loading;
  const resourceError = projects.error || versions.error || endpointRows.error || grids.error;
  const targetAxisActive = [xField, yField, zField].includes("target");
  const endpointSpecificOmit = [...Object.keys(commonFields), xField, yField, zField, "prompt", "model", "checkpoint_step", "sync_mode", "loras", "endpoint_id", "model_version_id", "checkpoint_revision_id"];
  return <Page title="Grid generator" subtitle="Choose the comparison axes, then create the grid.">
    <Notice error={resourceError} loading={loading} />
    <Panel title="Project context"><Field label="Project"><Dropdown aria-label="Project" value={project} options={[{ value: "", label: "Select…" }, ...rows(projects.data).map(row => ({ value: idOf(row), label: str(row.title ?? row.name) }))]} onChange={resetProject} /></Field>{project && <p className="vela-muted">All targets, cases, plans, cells, runs, and assessments stay inside this project.</p>}</Panel>
    {project && <Panel title="Generated grids"><Notice error={grids.error} loading={grids.loading} empty={!grids.loading && !rows(grids.data).length} emptyText="No generated grids yet." />{!!rows(grids.data).length && <div className="generated-grid-links">{rows(grids.data).map(grid => <a href={`#/grid/${encodeURIComponent(idOf(grid))}${routeQuery({ project })}`} key={idOf(grid)}><Grid3X3 size={18} /><span><strong>{str(grid.name, "Grid")}</strong><small>{str(grid.status, "pending")} · {str(grid.request_count, "0")} images</small></span><time dateTime={str(grid.created_at, "")}>{str(grid.created_at, "") ? new Date(str(grid.created_at)).toLocaleString() : ""}</time></a>)}</div>}</Panel>}
    {project && <>
    <Panel title="Axes first"><div className="form-grid"><Field label="X axis"><Dropdown aria-label="X axis" value={xField} options={axisFields.map(field => ({ value: field, label: field === "target" ? "Target" : field === "case" || field === "prompt" ? "Prompt / Case" : field.replaceAll("_", " ") }))} onChange={setXField} /></Field><Field label="Y axis"><Dropdown aria-label="Y axis" value={yField} options={axisFields.filter(field => field !== xField).map(field => ({ value: field, label: field === "target" ? "Target" : field === "case" || field === "prompt" ? "Prompt / Case" : field.replaceAll("_", " ") }))} onChange={setYField} /></Field><Field label="Z axis (optional)"><Dropdown aria-label="Z axis" value={zField} options={[{ value: "", label: "None" }, ...axisFields.filter(field => field !== xField && field !== yField).map(field => ({ value: field, label: field === "target" ? "Target" : field === "case" || field === "prompt" ? "Prompt / Case" : field.replaceAll("_", " ") }))]} onChange={setZField} /></Field></div>{xField !== "target" && <Field label={`${xField === "case" || xField === "prompt" ? "X prompt values" : `X ${xField} values`}`} hint={xField === "case" || xField === "prompt" ? "One inline immutable case is created for each line." : xSchema ? `${str(xSchema.type, "string")} · bounds/options come from every selected endpoint.` : "This field is not shared by all targets."}><textarea aria-label="X values" value={xText} onChange={event => setXText(event.target.value)} /></Field>}{yField !== "target" && <Field label={`${yField === "case" || yField === "prompt" ? "Y prompt values" : `Y ${yField} values`}`}><textarea aria-label="Y values" value={yText} onChange={event => setYText(event.target.value)} /></Field>}{zField && zField !== "target" && <Field label={`${zField === "case" || zField === "prompt" ? "Z prompt values" : `Z ${zField} values`}`}><textarea aria-label="Z values" value={zText} onChange={event => setZText(event.target.value)} /></Field>}{!activeCaseAxis && <Field label="Fixed prompt" hint="Fixed prompt is shown only because Prompt / Case is not an axis."><textarea aria-label="Fixed prompt" value={fixedPrompt} onChange={event => setFixedPrompt(event.target.value)} /></Field>}<TargetPicker targets={availableTargets} selected={targetKeys} axisActive={targetAxisActive} onChange={setTargetKeys} /></Panel>
    <Panel title="Shared controls"><p className="vela-muted">Only controls with a safe type, option, and bounds intersection across every selected endpoint appear here.</p><form key={JSON.stringify(Object.keys(commonFields))} onInput={event => setShared(parametersFromForm(new FormData(event.currentTarget), commonSchema))}><EndpointFields schema={commonSchema} omit={[xField, yField, zField]} /></form>{targets.map(target => <form key={target.target_id} className="grid-target-overrides" onInput={event => setOverrides(current => ({ ...current, [target.target_id]: parametersFromForm(new FormData(event.currentTarget), target.schema) }))}><strong>{target.endpoint_id} · {target.model_version_id ? `registered model ${target.model_version_id}` : "base endpoint"}</strong><EndpointFields schema={target.schema} omit={endpointSpecificOmit} /></form>)}</Panel>
    <Panel title="Generate"><button className="vela-button vela-button-primary grid-generate" disabled={Boolean(error) || busy} onClick={() => void generate()}>{busy ? "Preparing…" : <><Grid3X3 size={15} /> Create and queue grid</>}</button>{error && <div className="vela-notice vela-notice-error" role="status">{error}</div>}{queuedGridId && <a className="vela-button" href={`#/grid/${encodeURIComponent(queuedGridId)}${routeQuery({ project })}`}><ExternalLink size={15} /> View grid</a>}{message && <div className="vela-notice" role="status">{message}</div>}</Panel>
    </>}
  </Page>;
}

type GridAxisEntry = { value: unknown; label?: string; detail?: string; index?: number; sortValue?: number };
type CoordinateKey = "x" | "y" | "z";

export function gridTargetAxisEntry(value: unknown, index: number, cell?: Row): GridAxisEntry {
  const snapshot = (cell?.target_snapshot as Row | undefined) ?? {};
  const targetId = str(snapshot.target_id ?? value, String(value));
  const modelVersionName = str(cell?.model_version_name ?? snapshot.display_name, "");
  const checkpointStep = Number(cell?.checkpoint_step ?? snapshot.checkpoint_step);
  const sourceKind = str(cell?.model_version_source_kind ?? snapshot.model_version_source_kind, "training");
  const detail = sourceKind === "checkpoint_merge"
    ? [modelVersionName ? `Merge output: ${modelVersionName}` : "Merge output"].join("")
    : [modelVersionName ? `Checkpoint: ${modelVersionName}` : "Checkpoint", Number.isFinite(checkpointStep) ? `step ${checkpointStep}` : ""].filter(Boolean).join(" · ");
  return { value, label: targetId, detail, index };
}

export function sortGridAxisEntries(entries: GridAxisEntry[]) {
  return entries.length > 1 && entries.every(entry => Number.isFinite(entry.sortValue))
    ? [...entries].sort((left, right) => Number(left.sortValue) - Number(right.sortValue))
    : entries;
}

type GridBoardCell = Row | { coordinate?: { x: number; y: number }; target?: ExperimentTarget };

function gridCellAt(cells: GridBoardCell[], expected: Partial<Record<CoordinateKey, number>>) {
  return cells.find(item => {
    const row = item as Row;
    const coordinateValue = item && typeof item === "object" && "coordinate" in item ? item.coordinate : undefined;
    const coordinate = coordinateValue && typeof coordinateValue === "object" ? coordinateValue as Row : {};
    return Object.entries(expected).every(([key, value]) => Number(row[`${key}_index`] ?? coordinate[key]) === value);
  });
}

export function GridBoard({ x, y, cells, incomplete = false, completed = false, transpose = false, xLabel = "X", yLabel = "Y", coordinateKeys = { x: "x", y: "y" }, fixedCoordinates = {}, zoom = 0, imageHref }: {
  x: GridAxisEntry[];
  y: GridAxisEntry[];
  cells: GridBoardCell[];
  incomplete?: boolean;
  completed?: boolean;
  transpose?: boolean;
  xLabel?: string;
  yLabel?: string;
  coordinateKeys?: { x: CoordinateKey; y: CoordinateKey };
  fixedCoordinates?: Partial<Record<CoordinateKey, number>>;
  zoom?: number;
  imageHref?: (assetId: string) => string;
}) {
  const columns = transpose ? y : x;
  const tableRows = transpose ? x : y;
  const columnLabel = transpose ? yLabel : xLabel;
  const rowLabel = transpose ? xLabel : yLabel;
  const cellWidth = zoom === 1 ? "320px" : zoom === 2 ? "480px" : "720px";
  return (
    <Panel title={completed ? "Generated grid" : "Resolved cell preview"}>
      <div className="grid-board-frame">
        <div
          className="grid-board-region"
          data-fit={zoom === 0 ? "true" : "false"}
          style={zoom === 0 ? undefined : { "--grid-cell-width": cellWidth } as React.CSSProperties}
        >
          <table className={`grid-board ${completed ? "grid-board-completed" : "grid-board-preview"}`}>
            <caption>{completed ? "Generated grid" : "Resolved cells"}</caption>
            <thead>
              <tr>
                <th scope="col">{rowLabel} ↓ / {columnLabel} →</th>
                {columns.map((entry, index) => (
                  <th scope="col" key={`${entry.index ?? index}-${String(entry.value)}`}>
                    <span className="grid-axis-heading">
                      {String(entry.label ?? entry.value)}
                      {entry.detail && <small>{entry.detail}</small>}
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {tableRows.map((entry, rowIndex) => (
                <tr key={`${entry.index ?? rowIndex}-${String(entry.value)}`}>
                  <th scope="row">{String(entry.label ?? entry.value)}</th>
                  {columns.map((columnEntry, columnIndex) => {
                    const visualX = transpose ? entry.index ?? rowIndex : columnEntry.index ?? columnIndex;
                    const visualY = transpose ? columnEntry.index ?? columnIndex : entry.index ?? rowIndex;
                    const expected = { ...fixedCoordinates, [coordinateKeys.x]: visualX, [coordinateKeys.y]: visualY };
                    const cell = gridCellAt(cells, expected);
                    const row = cell as Row | undefined;
                    const target = cell && typeof cell === "object" && "target" in cell ? cell.target : undefined;
                    const assetId = str(row?.asset_revision_id ?? row?.asset_id);
                    const image = assetId ? (
                      <AssetImage
                        assetRevisionId={assetId}
                        maxPixels={zoom >= 2 ? 1024 : 512}
                        alt={str(row?.prompt_snapshot ?? row?.prompt, "Grid output")}
                        loading="lazy"
                        width={Number(row?.width) || undefined}
                        height={Number(row?.height) || undefined}
                      />
                    ) : null;
                    return (
                      <td key={columnIndex}>
                        <div className="grid-cell">
                          {incomplete ? "—" : completed && image ? (
                            imageHref ? (
                              <a className="grid-image-link" href={imageHref(assetId)} aria-label="Open grid output in image viewer">{image}</a>
                            ) : image
                          ) : completed ? (
                            str(row?.status, "pending")
                          ) : (
                            <div className="grid-cell-plan">
                              <b>{str(row?.status, "admissible")}</b>
                              <small>{str(row?.target_id ?? (target && typeof target === "object" && "target_id" in target ? target.target_id : undefined), "")}</small>
                            </div>
                          )}
                        </div>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </Panel>
  );
}

type ResultGridBoard = {
  key: string;
  x: GridAxisEntry[];
  y: GridAxisEntry[];
  xLabel: string;
  yLabel: string;
  transpose?: boolean;
  coordinateKeys?: { x: CoordinateKey; y: CoordinateKey };
  fixedCoordinates?: Partial<Record<CoordinateKey, number>>;
  heading?: string;
  fixedContext?: { label: string; value: string };
};

function gridExportSlice(board: ResultGridBoard, cells: GridBoardCell[]): GridExportSlice {
  const columns = board.transpose ? board.y : board.x;
  const tableRows = board.transpose ? board.x : board.y;
  const columnLabel = board.transpose ? board.yLabel : board.xLabel;
  const rowLabel = board.transpose ? board.xLabel : board.yLabel;
  const coordinateKeys = board.coordinateKeys ?? { x: "x", y: "y" };
  const fixedCoordinates = board.fixedCoordinates ?? {};
  return {
    title: board.heading ?? (board.fixedContext ? `${board.fixedContext.label}: ${board.fixedContext.value}` : undefined),
    cornerLabel: `${rowLabel} ↓ / ${columnLabel} →`,
    columns: columns.map(entry => ({ label: String(entry.label ?? entry.value), detail: entry.detail })),
    rows: tableRows.map(entry => ({ label: String(entry.label ?? entry.value), detail: entry.detail })),
    cells: tableRows.map((entry, rowIndex) => columns.map((columnEntry, columnIndex) => {
      const visualX = board.transpose ? entry.index ?? rowIndex : columnEntry.index ?? columnIndex;
      const visualY = board.transpose ? columnEntry.index ?? columnIndex : entry.index ?? rowIndex;
      const expected = { ...fixedCoordinates, [coordinateKeys.x]: visualX, [coordinateKeys.y]: visualY };
      const row = gridCellAt(cells, expected) as Row | undefined;
      return {
        assetRevisionId: str(row?.asset_revision_id ?? row?.asset_id) || undefined,
        status: str(row?.status, "No image"),
        alt: str(row?.prompt_snapshot ?? row?.prompt, "Grid output"),
      };
    })),
  };
}

function gridExportFilename(name: string, size: GridExportSize) {
  const slug = name.normalize("NFKD").replace(/[^\w]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase() || "grid";
  return `${slug}-${size === "compact" ? "small" : "full"}.png`;
}


export function GridScreen({ id, projectId = "" }: { id: string; projectId?: string }) {
  const [zoom, setZoom] = useState(0);
  const [exporting, setExporting] = useState<GridExportSize | null>(null);
  const [exportMessage, setExportMessage] = useState("");
  const resource = useResource<Row>(projectId ? `/api/projects/${encodeURIComponent(projectId)}/grids/${encodeURIComponent(id)}` : null);
  const data = resource.data;
  const cells = rows(data?.cells);
  const plan = data?.plan && typeof data.plan === "object" ? data.plan as Row : data;
  const axes = plan?.axes && typeof plan.axes === "object" ? plan.axes as Row : {};
  const axisName = (key: CoordinateKey) => str(axes[key] && typeof axes[key] === "object" ? (axes[key] as Row).name : "");
  const axisLabel = (name: string) => name === "target" ? "Target" : name.replaceAll("_", " ");
  const entries = (key: CoordinateKey) => {
    const axisValue = axes[key] && typeof axes[key] === "object" ? axes[key] as Row : {};
    const name = str(axisValue.name);
    const mapped = rows(axisValue.values).map((wrapped, index): GridAxisEntry => {
      const value = wrapped.value ?? wrapped;
      const cell = cells.find(item => Number((item.coordinate as Row | undefined)?.[key] ?? item[`${key}_index`]) === index);
      if (name === "target") return gridTargetAxisEntry(value, index, cell);
      if (name === "case" || name === "prompt") {
        const snapshot = cell?.case_snapshot && typeof cell.case_snapshot === "object" ? cell.case_snapshot as Row : {};
        const input = snapshot.input && typeof snapshot.input === "object" ? snapshot.input as Row : {};
        return { value, index, label: str(input.prompt ?? input.text, String(value)) };
      }
      const numeric = Number(value);
      return { value, index, label: String(value), sortValue: Number.isFinite(numeric) ? numeric : undefined };
    });
    return sortGridAxisEntries(mapped);
  };
  const x = entries("x");
  const y = entries("y");
  const z = entries("z");
  const xName = axisName("x");
  const yName = axisName("y");
  const zName = axisName("z");
  const transpose = x.length === 1 && y.length > 1;
  const resultBoards: ResultGridBoard[] = x.length === 1 && y.length > 1 && z.length
    ? [{
        key: "fixed-x",
        x: y,
        y: z,
        xLabel: axisLabel(yName),
        yLabel: axisLabel(zName),
        coordinateKeys: { x: "y", y: "z" },
        fixedCoordinates: { x: x[0].index ?? 0 },
        fixedContext: { label: axisLabel(xName), value: String(x[0].label ?? x[0].value) },
      }]
    : z.length
      ? z.map((entry, index) => ({
          key: `z-${entry.index ?? index}`,
          x,
          y,
          xLabel: axisLabel(xName),
          yLabel: axisLabel(yName),
          transpose,
          fixedCoordinates: { z: entry.index ?? index },
          heading: `${axisLabel(zName)} ${String(entry.label ?? entry.value)}`,
        }))
      : [{ key: "xy", x, y, xLabel: axisLabel(xName), yLabel: axisLabel(yName), transpose }];
  const returnHref = `#/grids${routeQuery({ project: projectId })}`;
  const imageHref = (assetId: string) => {
    const params = routeQuery({ project: projectId, category: "eval_output", asset: assetId, return: `grid/${id}${routeQuery({ project: projectId })}` });
    return `#/gallery${params}`;
  };
  const succeeded = cells.filter(cell => cell.status === "succeeded").length;
  const canExport = Boolean(data && cells.some(cell => str(cell.asset_revision_id ?? cell.asset_id)));

  async function exportGrid(size: GridExportSize) {
    if (!data || !canExport || exporting) return;
    setExporting(size);
    setExportMessage("");
    const name = str(data.name, "Grid results");
    try {
      await downloadGridImage({
        title: name,
        subtitle: `${str(data.status, "Generated")} · ${succeeded} / ${cells.length} images`,
        filename: gridExportFilename(name, size),
        size,
        slices: resultBoards.map(board => gridExportSlice(board, cells)),
        resolveImage: resolveAssetDelivery,
      });
      setExportMessage(`${size === "compact" ? "Small" : "Full-size"} grid image downloaded.`);
    } catch (reason) {
      setExportMessage(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setExporting(null);
    }
  }

  return (
    <div className="comparison-viewer vela-route-stage grid-results-view">
      <header className="viewer-topbar">
        <div>
          <h1>{str(data?.name, "Grid results")}</h1>
          <span>{str(data?.status, "Loading")} · {succeeded} / {cells.length} images</span>
        </div>
        <div className="viewer-actions">
          <div className="grid-export-controls" role="group" aria-label="Export grid image">
            <button type="button" className="vela-button" disabled={!canExport || Boolean(exporting)} title="Download a compact grid with 256 px image cells" onClick={() => void exportGrid("compact")}>
              <Download size={15} /> {exporting === "compact" ? "Exporting…" : "Export small"}
            </button>
            <button type="button" className="vela-button" disabled={!canExport || Boolean(exporting)} title="Download a grid using the original image dimensions" onClick={() => void exportGrid("full")}>
              <Download size={15} /> {exporting === "full" ? "Exporting…" : "Export full size"}
            </button>
          </div>
          <div className="grid-zoom-controls" role="group" aria-label="Grid zoom">
            <button type="button" aria-label="Zoom out" disabled={zoom === 0} onClick={() => setZoom(value => Math.max(0, value - 1))}><ZoomOut size={15} /></button>
            <button type="button" className={zoom === 0 ? "active" : ""} onClick={() => setZoom(0)}>Fit</button>
            <button type="button" aria-label="Zoom in" disabled={zoom === 3} onClick={() => setZoom(value => Math.min(3, value + 1))}><ZoomIn size={15} /></button>
          </div>
          <a className="vela-button" href={returnHref}><ArrowLeft size={15} /> Generated grids</a>
        </div>
      </header>
      <main className="vela-main">
        <Notice error={resource.error} loading={resource.loading} empty={!resource.loading && !data} emptyText="Grid result unavailable" />
        {exportMessage && <div className="vela-notice" role="status">{exportMessage}</div>}
        {data && resultBoards.map(board => (
          <section key={board.key} className={board.heading ? "grid-z-slice" : "grid-result-slice"}>
            {board.heading && <h2>{board.heading}</h2>}
            {board.fixedContext && <p className="grid-fixed-context"><strong>{board.fixedContext.label}:</strong> {board.fixedContext.value}</p>}
            <GridBoard
              x={board.x}
              y={board.y}
              cells={cells}
              completed
              transpose={board.transpose}
              xLabel={board.xLabel}
              yLabel={board.yLabel}
              coordinateKeys={board.coordinateKeys}
              fixedCoordinates={board.fixedCoordinates}
              zoom={zoom}
              imageHref={imageHref}
            />
          </section>
        ))}
      </main>
    </div>
  );
}
