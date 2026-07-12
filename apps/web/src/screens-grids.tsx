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
  { value: "prompt", label: "Prompt" }, { value: "seed", label: "Seed" },
  { value: "guidance", label: "Guidance" }, { value: "steps", label: "Steps" },
  { value: "lora_scale", label: "LoRA scale" }, { value: "checkpoint_step", label: "Checkpoint step" },
];

export function GridsScreen({ params }: { params: URLSearchParams }) {
  const initialProject = params.get("project") ?? "";
  const initialModel = params.get("model") ?? "";
  const initialVersion = params.get("model_version") ?? "";
  const [scope, setScope] = useState<"project" | "model">(params.get("scope") === "model" ? "model" : "project");
  const [projectId, setProjectId] = useState(initialProject);
  const [modelId, setModelId] = useState(initialModel);
  const [selected, setSelected] = useState<string[]>(initialVersion ? [initialVersion] : []);
  const [xField, setXField] = useState<GridAxisField>("prompt");
  const [yField, setYField] = useState<GridAxisField>("seed");
  const [xText, setXText] = useState("portrait\nprofile");
  const [yText, setYText] = useState("1\n2");
  const [xCheckpoints, setXCheckpoints] = useState<string[]>([]);
  const [yCheckpoints, setYCheckpoints] = useState<string[]>([]);
  const [zEnabled, setZEnabled] = useState(false);
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
  const modelFamilies = useResource<unknown>(projectId ? `/api/models?project_id=${encodeURIComponent(projectId)}` : null);
  const versions = useResource<unknown>(projectId ? `/api/model-versions?project_id=${encodeURIComponent(projectId)}` : null);
  const queue = useResource<unknown>(`/api/generation-queue${query({ project_id: projectId || undefined })}`);
  const generationModels = useMemo(() => rows(versions.data).map(generationModelFromRow).filter(model => model.projectId === projectId && (scope !== "model" || !modelId || model.modelId === modelId)), [versions.data, projectId, scope, modelId]);
  useEffect(() => setSelected(ids => ids.filter(id => generationModels.some(model => model.id === id))), [generationModels]);
  useEffect(() => { setXCheckpoints(ids => ids.filter(id => generationModels.some(model => model.id === id))); setYCheckpoints(ids => ids.filter(id => generationModels.some(model => model.id === id))); }, [generationModels]);
  const selectedModels = selected.map(id => generationModels.find(model => model.id === id)).filter((model): model is GenerationModel => Boolean(model));
  const x = gridAxis(xField, xText, xCheckpoints, generationModels);
  const y = gridAxis(yField, yText, yCheckpoints, generationModels);
  let preview: GridCellPlan[] = [];
  let previewError = "";
  try { preview = compileGridPlan({ x, y, availableModels: generationModels, zModels: zEnabled ? selectedModels : undefined, defaultModel: selectedModels[0], fixedPrompt: xField === "prompt" || yField === "prompt" ? undefined : fixedPrompt, base: { numImages: 1, seed, width, height, guidance, steps, loraScale } }); } catch (reason) { previewError = reason instanceof Error ? reason.message : String(reason); }

  async function generate() {
    if (submitting) return;
    setError("");
    if (!projectId) { setError("Select a project."); return; }
    if (scope === "model" && !modelId) { setError("Select a model for Model scope."); return; }
    if (!preview.length) { setError(previewError || "Complete the grid definition."); return; }
    if (!zEnabled && selectedModels.length !== 1) { setError("Select exactly one checkpoint, or enable the Z model axis."); return; }
    if (preview.some(cell => !cell.model.available || cell.model.provider !== "fal")) { setError("Every selected checkpoint must have a usable FAL registration."); return; }
    setSubmitting(true);
    try {
      if (!attempt.current || attempt.current.children.length !== preview.length) attempt.current = { parent: crypto.randomUUID(), children: preview.map(() => crypto.randomUUID()) };
      const ids = attempt.current;
      const context: GenerationContext = scope === "model" ? { kind: "model", projectId, modelId } : { kind: "project", projectId };
      const compiled = [];
      for (const cell of preview) {
        const requestId = ids.children[cell.ordinal];
        const child = await compileGenerationChild({ workflow: "grid", context, model: cell.model, parameters: cell.parameters, clientRequestId: requestId });
        compiled.push({ model_version_id: child.modelVersionId, compiled_request_id: child.compiledRequestId, admission_id: child.admissionId });
      }
      const uniqueModels = Array.from(new Set(preview.map(cell => cell.model.id)));
      const queued = await api<Row>("/api/generation-queue", jsonBody({ workflow: "grid", provider: "fal",
        context: { kind: context.kind, project_id: projectId, model_id: context.kind === "model" ? context.modelId : undefined },
        model_version_ids: uniqueModels, compiled_request_id: ids.parent, client_request_id: ids.parent,
        request: { workflow: "grid", context, models: uniqueModels.map(id => generationModels.find(model => model.id === id)),
          axes: { x, y, z: zEnabled ? { field: "model", values: selectedModels.map(model => ({ id: model.id, label: `${model.name} · ${model.checkpointLabel}`, value: model.id })) } : null },
          cells: preview.map(cell => ({ id: cell.id, ordinal: cell.ordinal, x_index: cell.xIndex, y_index: cell.yIndex, z_index: cell.zIndex, x: cell.x, y: cell.y, z: cell.z, model_version_id: cell.model.id, parameters: cell.parameters })) },
        children: compiled }));
      setQueuedId(idOf(queued)); attempt.current = null; await queue.reload();
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setSubmitting(false); }
  }

  return <Page title="Grid generator" subtitle="Build deterministic X/Y grids and optional model/checkpoint Z grids through the shared generation queue.">
    <Notice error={projects.error || modelFamilies.error || versions.error || queue.error} loading={projects.loading || modelFamilies.loading || versions.loading} />
    <Panel title="Generation context"><div className="form-grid">
      <Field label="Scope"><select value={scope} onChange={event => { setScope(event.target.value as "project" | "model"); setSelected([]); }}><option value="project">Project</option><option value="model">Model / checkpoint</option></select></Field>
      <Field label="Project"><select value={projectId} onChange={event => { setProjectId(event.target.value); setModelId(""); setSelected([]); }}><option value="">Select...</option>{rows(projects.data).map(project => <option value={idOf(project)} key={idOf(project)}>{str(project.title ?? project.name)}</option>)}</select></Field>
      {scope === "model" && <Field label="Model"><select value={modelId} onChange={event => { setModelId(event.target.value); setSelected([]); }}><option value="">Select...</option>{rows(modelFamilies.data).map(model => <option value={idOf(model)} key={idOf(model)}>{str(model.name)}</option>)}</select></Field>}
    </div><ModelCheckpointPicker models={generationModels} selected={selected} onChange={setSelected} />
      <label className="grid-z-toggle"><input type="checkbox" checked={zEnabled} onChange={event => setZEnabled(event.target.checked)} /> Use selected checkpoints as Z axis (one X/Y grid per checkpoint)</label>
    </Panel>
    <Panel title="Global generation settings"><div className="form-grid">
      {xField !== "prompt" && yField !== "prompt" ? <Field label="Prompt" hint="Used by every cell because Prompt is not an axis."><textarea value={fixedPrompt} onChange={event => setFixedPrompt(event.target.value)} required /></Field> : <div className="vela-notice"><strong>Prompt axis active</strong><span>Each Prompt-axis value supplies the cell prompt; the fixed prompt is disabled.</span></div>}
      <Field label="Provider"><input readOnly value={selectedModels.length ? "Derived from selected checkpoints (FAL)" : "Select checkpoints"} /></Field>
      <GlobalNumber label="Seed" value={seed} onChange={setSeed} varied={xField === "seed" || yField === "seed"} />
      <GlobalNumber label="Width" value={width} onChange={setWidth} />
      <GlobalNumber label="Height" value={height} onChange={setHeight} />
      <GlobalNumber label="Guidance" value={guidance} onChange={setGuidance} varied={xField === "guidance" || yField === "guidance"} step="0.1" />
      <GlobalNumber label="Steps" value={steps} onChange={setSteps} varied={xField === "steps" || yField === "steps"} />
      <GlobalNumber label="LoRA scale" value={loraScale} onChange={setLoraScale} varied={xField === "lora_scale" || yField === "lora_scale"} step="0.1" />
    </div></Panel>
    <div className="grid-axis-layout"><AxisEditor title="X axis · columns" field={xField} onField={setXField} text={xText} onText={setXText} selectedCheckpoints={xCheckpoints} onCheckpoints={setXCheckpoints} models={generationModels} /><AxisEditor title="Y axis · rows" field={yField} onField={setYField} text={yText} onText={setYText} selectedCheckpoints={yCheckpoints} onCheckpoints={setYCheckpoints} models={generationModels} /></div>
    {previewError && <div className="vela-notice vela-notice-error" role="alert"><strong>Grid definition</strong><span>{previewError}</span></div>}
    {!previewError && <GridPreview cells={preview} x={x} y={y} zEnabled={zEnabled} />}
    {error && <div className="vela-notice vela-notice-error" role="alert"><strong>Cannot generate grid</strong><span>{error}</span></div>}
    <button className="vela-button vela-button-primary grid-generate" type="button" disabled={submitting} onClick={() => void generate()}>{submitting ? <><LoaderCircle className="vela-spin" size={15} /> Preparing grid...</> : <><Grid3X3 size={15} /> Generate grid</>}</button>
    {queuedId && <div className="vela-notice" role="status"><strong>Grid queued</strong><span><a href={`#/grid/${queuedId}`}>Open grid {queuedId}</a></span></div>}
    <GenerationQueue items={rows(queue.data).filter(row => row.workflow === "grid")} error={queue.error} loading={queue.loading} />
  </Page>;
}

function AxisEditor({ title, field, onField, text, onText, selectedCheckpoints, onCheckpoints, models }: { title: string; field: GridAxisField; onField: (field: GridAxisField) => void; text: string; onText: (text: string) => void; selectedCheckpoints: string[]; onCheckpoints: (ids: string[]) => void; models: GenerationModel[] }) {
  return <Panel title={title}><Field label="Parameter"><select value={field} onChange={event => onField(event.target.value as GridAxisField)}>{FIELDS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></Field>
    {field === "checkpoint_step" ? <ModelCheckpointPicker models={models} selected={selectedCheckpoints} onChange={onCheckpoints} /> : <Field label="Values" hint="One value per line. Order is preserved; duplicate labels remain separate cells."><textarea value={text} onChange={event => onText(event.target.value)} /></Field>}
  </Panel>;
}

function GlobalNumber({ label, value, onChange, varied = false, step = "1" }: { label: string; value: number; onChange: (value: number) => void; varied?: boolean; step?: string }) {
  return <Field label={label} hint={varied ? `${label} is controlled by an axis.` : undefined}><input type="number" step={step} value={value} disabled={varied} onChange={event => onChange(Number(event.target.value))} /></Field>;
}

function gridAxis(field: GridAxisField, text: string, selected: string[], models: GenerationModel[]): GridAxis {
  if (field !== "checkpoint_step") return { field, values: axisValues(text) };
  return { field, values: selected.map((id, index) => { const model = models.find(item => item.id === id); return { id: `checkpoint-${index}-${id}`, label: model ? `${model.name} · ${model.checkpointLabel}` : id, value: id }; }) };
}

function GridPreview({ cells, x, y, zEnabled }: { cells: GridCellPlan[]; x: GridAxis; y: GridAxis; zEnabled: boolean }) {
  const groups = Array.from(new Set(cells.map(cell => cell.zIndex)));
  return <Panel title="Grid preview"><div className="grid-preview-tools"><span>X · {x.field} · columns</span><span>Y · {y.field} · rows</span><span>{cells.length} cells</span></div>
    <div className="grid-subgrids">{groups.map(zIndex => { const group = cells.filter(cell => cell.zIndex === zIndex); return <section key={zIndex}>{zEnabled && <h3>{group[0]?.z?.label}</h3>}<GridBoard x={x.values} y={y.values} cells={group} /></section>; })}</div>
  </Panel>;
}

function GridBoard({ x, y, cells, completed = false }: { x: GridAxisValue[]; y: GridAxisValue[]; cells: Array<GridCellPlan | Row>; completed?: boolean }) {
  return <div className="grid-board" style={{ "--cell": "82px", gridTemplateColumns: `100px repeat(${Math.max(1, x.length)}, var(--cell))` } as CSSProperties}><span />
    {x.map((entry, index) => <strong key={`x-${index}-${entry.id}`}>{entry.label}</strong>)}
    {y.flatMap((entry, yIndex) => [<strong key={`y-${yIndex}-${entry.id}`}>{entry.label}</strong>, ...x.map((_, xIndex) => { const cell = cells.find(item => Number((item as Row).x_index ?? (item as GridCellPlan).xIndex) === xIndex && Number((item as Row).y_index ?? (item as GridCellPlan).yIndex) === yIndex); const row = cell as Row | undefined; const assetId = completed ? str(row?.asset_id ?? row?.asset_revision_id, "") : ""; return <div className="grid-cell" key={`cell-${yIndex}-${xIndex}`}>{assetId ? <AssetImage assetRevisionId={assetId} loading="lazy" alt={`${xIndex}, ${yIndex}`} /> : <span>{completed ? "Pending" : `${xIndex + 1} × ${yIndex + 1}`}</span>}</div>; })])}
  </div>;
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
  return <div className="comparison-viewer vela-route-stage"><header className="viewer-topbar"><div><strong>{str(data?.name, "Grid results")}</strong><span>{isLegacy ? "Historical · read only" : str(data?.status, "Loading")} · {cells.length} cells</span></div><div className="viewer-actions"><a className="vela-button" href="#/grids"><ArrowLeft size={15} /> Grid generator</a></div></header><main className="vela-main">
    <Notice error={error} loading={queue.loading || legacy.loading} />{isLegacy && <div className="vela-notice"><strong>Historical grid</strong><span>This saved GridDefinition remains available for review. New runs use the shared Grid generator and queue.</span></div>}<Panel title="Grid review"><Status value={isLegacy ? "saved" : str(data?.status, "queued")} /><div className="grid-subgrids">{zValues.map((z, zIndex) => <section key={`${z.id}-${zIndex}`}><h3>{z.label}</h3><GridBoard x={x.values} y={y.values} cells={cells.filter(cell => Number(cell.z_index ?? 0) === zIndex)} completed /></section>)}</div>
      {cells.filter(cell => cell.asset_id).map(cell => { const cellX = cell.x && typeof cell.x === "object" ? cell.x as Row : {}; const cellY = cell.y && typeof cell.y === "object" ? cell.y as Row : {}; return <a key={str(cell.id)} className="vela-button" href={galleryOpenHref({ ...cell, project_id: data?.project_id, origin_type: "EVAL" })}><ExternalLink size={15} /> Open {str(cellX.label ?? cell.x_value, "cell")} × {str(cellY.label ?? cell.y_value, "cell")}</a>; })}</Panel>
  </main></div>;
}

function legacyAxis(value: unknown, fallback: GridAxisField): GridAxis {
  const axis = value && typeof value === "object" ? value as Row : {};
  const field = str(axis.parameter ?? axis.field, fallback) as GridAxisField;
  const values = Array.isArray(axis.values) ? axis.values : [];
  return { field, values: values.map((entry, index) => ({ id: `legacy-${field}-${index}`, label: str(entry), value: typeof entry === "number" ? entry : str(entry) })) };
}
