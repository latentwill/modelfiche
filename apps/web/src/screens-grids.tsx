import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { ArrowLeft, ExternalLink, Grid3X3, LoaderCircle } from "lucide-react";
import { api, idOf, jsonBody, listOf, query, str } from "./api";
import { AssetImage } from "./asset-image";
import { useResource } from "./hooks";
import { GenerationQueue, ModelCheckpointPicker } from "./generator";
import { compileGenerationChild, generationModelFromRow, type GenerationContext, type GenerationModel } from "./generation";
import { axisValues, compileGridPlan, type GridAxis, type GridAxisField, type GridAxisValue, type GridCellPlan } from "./grid";
import { galleryOpenHref } from "./screens-assets";
import { Field, Notice, Page, Panel, Status } from "./ui";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);
const FIELDS: Array<{ value: GridAxisField; label: string }> = [
  { value: "prompt", label: "Prompt" },
  { value: "seed", label: "Seed" },
  { value: "guidance", label: "Guidance" },
  { value: "steps", label: "Steps" },
  { value: "lora_scale", label: "LoRA scale" },
  { value: "checkpoint_step", label: "Model / checkpoint" },
];
const modelAxisField = (field: GridAxisField) => field === "model" || field === "checkpoint_step";

export function GridsScreen({ params }: { params: URLSearchParams }) {
  const initialProject = params.get("project") ?? "";
  const initialVersion = params.get("model_version") ?? "";
  const [projectId, setProjectId] = useState(initialProject);
  const [fixedModelId, setFixedModelId] = useState(initialVersion);
  const [xField, setXField] = useState<GridAxisField>("prompt");
  const [yField, setYField] = useState<GridAxisField>("seed");
  const [zEnabled, setZEnabled] = useState(false);
  const [zField, setZField] = useState<GridAxisField>("guidance");
  const [xText, setXText] = useState("portrait\nprofile");
  const [yText, setYText] = useState("1\n2");
  const [zText, setZText] = useState("3\n5");
  const [xModels, setXModels] = useState<string[]>([]);
  const [yModels, setYModels] = useState<string[]>([]);
  const [zModels, setZModels] = useState<string[]>([]);
  const [fixedPrompt, setFixedPrompt] = useState("");
  const [seed, setSeed] = useState(1);
  const [width, setWidth] = useState(1024);
  const [height, setHeight] = useState(1024);
  const [guidance, setGuidance] = useState(3.5);
  const [steps, setSteps] = useState(28);
  const [loraScale, setLoraScale] = useState(1);
  const [error, setError] = useState("");
  const [queuedId, setQueuedId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const attempt = useRef<{ parent: string; children: string[] } | null>(null);
  const projects = useResource<unknown>("/api/projects?limit=100");
  const versions = useResource<unknown>(projectId ? `/api/model-versions?project_id=${encodeURIComponent(projectId)}` : null);
  const queue = useResource<unknown>(`/api/generation-queue${query({ project_id: projectId || undefined })}`);
  const generationModels = useMemo(() => rows(versions.data).map(generationModelFromRow).filter(model => model.projectId === projectId), [versions.data, projectId]);
  useEffect(() => {
    if (versions.data == null) return;
    const valid = new Set(generationModels.map(model => model.id));
    setFixedModelId(id => valid.has(id) ? id : "");
    setXModels(ids => ids.filter(id => valid.has(id)));
    setYModels(ids => ids.filter(id => valid.has(id)));
    setZModels(ids => ids.filter(id => valid.has(id)));
  }, [generationModels, versions.data]);
  const fixedModel = generationModels.find(model => model.id === fixedModelId);
  const x = gridAxis(xField, xText, xModels, generationModels);
  const y = gridAxis(yField, yText, yModels, generationModels);
  const z = zEnabled ? gridAxis(zField, zText, zModels, generationModels) : undefined;
  let preview: GridCellPlan[] = [];
  let previewError = "";
  try {
    preview = compileGridPlan({
      x,
      y,
      z,
      availableModels: generationModels,
      defaultModel: fixedModel,
      fixedPrompt: xField === "prompt" || yField === "prompt" || zField === "prompt" && zEnabled ? undefined : fixedPrompt,
      base: { numImages: 1, seed, width, height, guidance, steps, loraScale },
    });
  } catch (reason) {
    previewError = reason instanceof Error ? reason.message : String(reason);
  }

  async function generate() {
    if (submitting) return;
    setError("");
    if (!projectId) { setError("Select a project."); return; }
    if (!preview.length) { setError(previewError || "Complete the grid definition."); return; }
    if (preview.some(cell => !cell.model.available || cell.model.provider !== "fal")) { setError("Every selected checkpoint must have a usable FAL registration."); return; }
    setSubmitting(true);
    try {
      if (!attempt.current || attempt.current.children.length !== preview.length) attempt.current = { parent: crypto.randomUUID(), children: preview.map(() => crypto.randomUUID()) };
      const ids = attempt.current;
      const context: GenerationContext = { kind: "project", projectId };
      const compiled = [];
      for (const cell of preview) {
        const child = await compileGenerationChild({ workflow: "grid", context, model: cell.model, parameters: cell.parameters, clientRequestId: ids.children[cell.ordinal] });
        compiled.push({ model_version_id: child.modelVersionId, compiled_request_id: child.compiledRequestId, admission_id: child.admissionId });
      }
      const uniqueModels = Array.from(new Set(preview.map(cell => cell.model.id)));
      const queued = await api<Row>("/api/generation-queue", jsonBody({
        workflow: "grid", provider: "fal", context: { kind: "project", project_id: projectId }, model_version_ids: uniqueModels,
        compiled_request_id: ids.parent, client_request_id: ids.parent,
        request: { workflow: "grid", context, models: uniqueModels.map(id => generationModels.find(model => model.id === id)), axes: { x, y, z: z ?? null },
          cells: preview.map(cell => ({ id: cell.id, ordinal: cell.ordinal, x_index: cell.xIndex, y_index: cell.yIndex, z_index: cell.zIndex, x: cell.x, y: cell.y, z: cell.z, model_version_id: cell.model.id, parameters: cell.parameters })) },
        children: compiled,
      }));
      setQueuedId(idOf(queued));
      attempt.current = null;
      await queue.reload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSubmitting(false);
    }
  }

  const changeField = (setter: (field: GridAxisField) => void, selectedSetter: (ids: string[]) => void, textSetter: (text: string) => void, field: GridAxisField) => {
    setter(field);
    selectedSetter([]);
    if (modelAxisField(field)) textSetter("");
  };
  return <Page title="Grid generator" subtitle="Compose deterministic X/Y columns and rows with an optional Z subgrid through the shared generation queue.">
    <Notice error={projects.error || versions.error || queue.error} loading={projects.loading || versions.loading} />
    <Panel title="Fixed / global settings">
      <div className="form-grid">
        <Field label="Project"><select value={projectId} onChange={event => { setProjectId(event.target.value); setFixedModelId(""); }}><option value="">Select...</option>{rows(projects.data).map(project => <option value={idOf(project)} key={idOf(project)}>{str(project.title ?? project.name)}</option>)}</select></Field>
        <Field label="Fixed prompt"><textarea value={fixedPrompt} disabled={xField === "prompt" || yField === "prompt" || zField === "prompt" && zEnabled} onChange={event => setFixedPrompt(event.target.value)} /></Field>
        <ModelCheckpointPicker mode="fixed" models={generationModels} selected={fixedModelId ? [fixedModelId] : []} onChange={ids => setFixedModelId(ids[0] ?? "")} label="Fixed model / checkpoint" />
        <GlobalNumber label="Seed" value={seed} onChange={setSeed} varied={xField === "seed" || yField === "seed" || zField === "seed" && zEnabled} />
        <GlobalNumber label="Width" value={width} onChange={setWidth} />
        <GlobalNumber label="Height" value={height} onChange={setHeight} />
        <GlobalNumber label="Guidance" value={guidance} onChange={setGuidance} varied={xField === "guidance" || yField === "guidance" || zField === "guidance" && zEnabled} step="0.1" />
        <GlobalNumber label="Steps" value={steps} onChange={setSteps} varied={xField === "steps" || yField === "steps" || zField === "steps" && zEnabled} />
        <GlobalNumber label="LoRA scale" value={loraScale} onChange={setLoraScale} varied={xField === "lora_scale" || yField === "lora_scale" || zField === "lora_scale" && zEnabled} step="0.1" />
      </div>
    </Panel>
    <div className="grid-axis-layout">
      <AxisEditor title="X axis · columns" field={xField} onField={field => changeField(setXField, setXModels, setXText, field)} text={xText} onText={setXText} selectedModels={xModels} onModels={setXModels} models={generationModels} />
      <AxisEditor title="Y axis · rows" field={yField} onField={field => changeField(setYField, setYModels, setYText, field)} text={yText} onText={setYText} selectedModels={yModels} onModels={setYModels} models={generationModels} />
    </div>
    <Panel title="Z axis · optional subgrids">
      <label className="grid-z-toggle"><input type="checkbox" checked={zEnabled} onChange={event => setZEnabled(event.target.checked)} /> Enable Z axis</label>
      {zEnabled && <AxisEditor title="Z axis · subgrids" field={zField} onField={field => changeField(setZField, setZModels, setZText, field)} text={zText} onText={setZText} selectedModels={zModels} onModels={setZModels} models={generationModels} embedded />}
      {!zEnabled && <p className="vela-meta">One X/Y grid is shown. Enable Z to compose one ordered subgrid per value.</p>}
    </Panel>
    {previewError && <div className="vela-notice vela-notice-error" role="alert"><strong>Grid incomplete</strong><span>{previewError}</span></div>}
    <GridPreview cells={preview} x={x} y={y} z={z} incomplete={Boolean(previewError)} />
    {error && <div className="vela-notice vela-notice-error" role="alert"><strong>Cannot generate grid</strong><span>{error}</span></div>}
    <button className="vela-button vela-button-primary grid-generate" type="button" disabled={submitting} onClick={() => void generate()}>{submitting ? <><LoaderCircle className="vela-spin" size={15} /> Preparing grid...</> : <><Grid3X3 size={15} /> Generate grid</>}</button>
    {queuedId && <div className="vela-notice" role="status"><strong>Grid queued</strong><span><a href={`#/grid/${queuedId}`}>Open grid {queuedId}</a></span></div>}
    <GenerationQueue items={rows(queue.data).filter(row => row.workflow === "grid")} error={queue.error} loading={queue.loading} />
  </Page>;
}

function AxisEditor({ title, field, onField, text, onText, selectedModels, onModels, models, embedded = false }: { title: string; field: GridAxisField; onField: (field: GridAxisField) => void; text: string; onText: (text: string) => void; selectedModels: string[]; onModels: (ids: string[]) => void; models: GenerationModel[]; embedded?: boolean }) {
  const body = <><Field label="Parameter"><select value={field} onChange={event => onField(event.target.value as GridAxisField)}>{FIELDS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></Field>{modelAxisField(field) ? <ModelCheckpointPicker mode="axis" models={models} selected={selectedModels} onChange={onModels} label="Axis model / checkpoints" /> : <Field label="Values"><textarea value={text} onChange={event => onText(event.target.value)} /></Field>}</>;
  return embedded ? <div className="grid-axis-editor">{body}</div> : <Panel title={title}>{body}</Panel>;
}

function GlobalNumber({ label, value, onChange, varied = false, step = "1" }: { label: string; value: number; onChange: (value: number) => void; varied?: boolean; step?: string }) {
  return <Field label={label}><input type="number" step={step} value={value} disabled={varied} onChange={event => onChange(Number(event.target.value))} /></Field>;
}

function gridAxis(field: GridAxisField, text: string, selected: string[], models: GenerationModel[]): GridAxis {
  if (!modelAxisField(field)) return { field, values: axisValues(text) };
  return { field, values: selected.map(id => { const model = models.find(item => item.id === id); return { id: `model-${id}`, label: model ? `${model.name} · ${model.checkpointLabel}` : id, value: id }; }) };
}

function GridPreview({ cells, x, y, z, incomplete }: { cells: GridCellPlan[]; x: GridAxis; y: GridAxis; z?: GridAxis; incomplete: boolean }) {
  const groups = z ? Array.from(new Set(cells.map(cell => cell.zIndex))) : [0];
  return <Panel title="Composed grid preview"><div className="grid-preview-tools"><span>X · {x.field} · columns</span><span>Y · {y.field} · rows</span><span>{z ? `Z · ${z.field} · ${groups.length} subgrids` : "One X/Y grid"}</span><span>{cells.length} cells</span></div>{incomplete ? <div className="vela-empty" role="status">Complete the fixed settings and axis values to preview composed cells.</div> : <div className="grid-subgrids">{groups.map(zIndex => { const group = cells.filter(cell => cell.zIndex === zIndex); return <section key={zIndex}>{z && <h3>{group[0]?.z?.label}</h3>}<GridBoard x={x.values} y={y.values} cells={group} /></section>; })}</div>}</Panel>;
}

function GridBoard({ x, y, cells, completed = false }: { x: GridAxisValue[]; y: GridAxisValue[]; cells: Array<GridCellPlan | Row>; completed?: boolean }) {
  return <div className="grid-board" style={{ "--cell": "82px", gridTemplateColumns: `100px repeat(${Math.max(1, x.length)}, var(--cell))` } as CSSProperties}><span />{x.map((entry, index) => <strong key={`x-${index}-${entry.id}`}>{entry.label}</strong>)}{y.flatMap((entry, yIndex) => [<strong key={`y-${yIndex}-${entry.id}`}>{entry.label}</strong>, ...x.map((_, xIndex) => { const cell = cells.find(item => Number((item as Row).x_index ?? (item as GridCellPlan).xIndex) === xIndex && Number((item as Row).y_index ?? (item as GridCellPlan).yIndex) === yIndex); const row = cell as Row | undefined; const assetId = completed ? str(row?.asset_id ?? row?.asset_revision_id, "") : ""; return <div className="grid-cell" key={`cell-${yIndex}-${xIndex}`}>{assetId ? <AssetImage assetRevisionId={assetId} loading="lazy" alt={`${xIndex}, ${yIndex}`} /> : <span>{completed ? "Pending" : `${xIndex + 1} × ${yIndex + 1}`}</span>}</div>; })])}</div>;
}

export function GridScreen({ id }: { id: string }) {
  const queue = useResource<Row>(`/api/generation-queue/${id}/grid-results`);
  const legacy = useResource<Row>(queue.error ? `/api/grids/${id}` : null);
  const isLegacy = !queue.data && Boolean(legacy.data);
  const data = queue.data ?? legacy.data;
  const request = queue.data?.request as Row | undefined;
  const axes = request?.axes as Row | undefined;
  const legacyX = legacyAxis(legacy.data?.x_axis, "prompt");
  const legacyY = legacyAxis(legacy.data?.y_axis, "seed");
  const x = (axes?.x as GridAxis | undefined) ?? legacyX;
  const y = (axes?.y as GridAxis | undefined) ?? legacyY;
  const cells = rows(data?.cells);
  const zValues = Array.isArray((axes?.z as Row | undefined)?.values) ? (axes?.z as Row).values as GridAxisValue[] : [{ id: "default", label: isLegacy ? "Historical grid" : "Grid", value: "" }];
  const error = queue.error && legacy.error ? `Queue lookup: ${queue.error}. Historical grid lookup: ${legacy.error}.` : "";
  return <div className="comparison-viewer vela-route-stage"><header className="viewer-topbar"><div><strong>{str(data?.name, "Grid results")}</strong><span>{isLegacy ? "Historical · read only" : str(data?.status, "Loading")} · {cells.length} cells</span></div><div className="viewer-actions"><a className="vela-button" href="#/grids"><ArrowLeft size={15} /> Grid generator</a></div></header><main className="vela-main"><Notice error={error} loading={queue.loading || legacy.loading} />{isLegacy && <div className="vela-notice"><strong>Historical grid</strong><span>This saved GridDefinition remains available for review. New runs use the shared Grid generator and queue.</span></div>}<Panel title="Grid review"><Status value={isLegacy ? "saved" : str(data?.status, "queued")} /><div className="grid-subgrids">{zValues.map((z, zIndex) => <section key={`${z.id}-${zIndex}`}><h3>{z.label}</h3><GridBoard x={x.values} y={y.values} cells={cells.filter(cell => Number(cell.z_index ?? 0) === zIndex)} completed /></section>)}</div>{cells.filter(cell => cell.asset_id).map(cell => { const cellX = cell.x && typeof cell.x === "object" ? cell.x as Row : {}; const cellY = cell.y && typeof cell.y === "object" ? cell.y as Row : {}; return <a key={str(cell.id)} className="vela-button" href={galleryOpenHref({ ...cell, project_id: data?.project_id, origin_type: "EVAL" })}><ExternalLink size={15} /> Open {str(cellX.label ?? cell.x_value, "cell")} × {str(cellY.label ?? cell.y_value, "cell")}</a>; })}</Panel></main></div>;
}

function legacyAxis(value: unknown, fallback: GridAxisField): GridAxis {
  const axis = value && typeof value === "object" ? value as Row : {};
  const field = str(axis.parameter ?? axis.field, fallback) as GridAxisField;
  const values = Array.isArray(axis.values) ? axis.values : [];
  return { field, values: values.map((entry, index) => ({ id: `legacy-${field}-${index}`, label: str(entry), value: typeof entry === "number" ? entry : str(entry) })) };
}
