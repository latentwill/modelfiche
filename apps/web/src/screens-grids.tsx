import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { ArrowLeft, ExternalLink, Grid3X3, LoaderCircle } from "lucide-react";
import { api, idOf, jsonBody, listOf, query, str } from "./api";
import { AssetImage } from "./asset-image";
import { pollWhileActive, useResource } from "./hooks";
import { EndpointFields, GenerationQueue, ModelCheckpointPicker, parametersFromForm } from "./generator";
import { compileGenerationChild, connectedFalGenerationModels, type GenerationContext, type GenerationModel } from "./generation";
import { compileGridPlan, parseGridAxisValues, type GridAxis, type GridAxisField, type GridAxisSchema, type GridAxisValue, type GridCellPlan } from "./grid";
import { galleryOpenHref } from "./screens-assets";
import { Dropdown, Field, Notice, Page, Panel, Status } from "./ui";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);
const PROGRESS_POLL = { pollInterval: 1000, pollWhile: pollWhileActive };
const modelAxisField = (field: GridAxisField) => field === "model" || field === "checkpoint_step";

export function GridsScreen({ params }: { params: URLSearchParams }) {
  const initialProject = params.get("project") ?? "";
  const initialVersion = params.get("model_version") ?? "";
  const [projectId, setProjectId] = useState(initialProject);
  const [fixedModelId, setFixedModelId] = useState(initialVersion);
  const [promptSetId, setPromptSetId] = useState(params.get("prompt_set") ?? "");
  const [xField, setXField] = useState<GridAxisField>("");
  const [yField, setYField] = useState<GridAxisField>("");
  const [zEnabled, setZEnabled] = useState(false);
  const [zField, setZField] = useState<GridAxisField>("");
  const [xText, setXText] = useState("");
  const [yText, setYText] = useState("");
  const [zText, setZText] = useState("");
  const [xModels, setXModels] = useState<string[]>([]);
  const [yModels, setYModels] = useState<string[]>([]);
  const [zModels, setZModels] = useState<string[]>([]);
  const [fixedPrompt, setFixedPrompt] = useState("");
  const [fixedRevision, setFixedRevision] = useState(0);
  const [fixedFormValid, setFixedFormValid] = useState(false);
  const [error, setError] = useState("");
  const [queuedId, setQueuedId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const attempt = useRef<{ parent: string; children: string[]; gridId?: string } | null>(null);
  const fixedFormRef = useRef<HTMLFormElement>(null);
  const initializedEndpoint = useRef("");
  const projects = useResource<unknown>("/api/projects?limit=100");
  const versions = useResource<unknown>(projectId ? `/api/model-versions?project_id=${encodeURIComponent(projectId)}` : null);
  const promptSets = useResource<unknown>(projectId ? `/api/prompt-sets?project_id=${encodeURIComponent(projectId)}` : null);
  const promptSet = useResource<Row>(promptSetId ? `/api/prompt-sets/${promptSetId}` : null);
  const savedGrids = useResource<unknown>(projectId ? `/api/grids?project_id=${encodeURIComponent(projectId)}` : null);
  const queue = useResource<unknown>(`/api/generation-queue${query({ project_id: projectId || undefined })}`, PROGRESS_POLL);
  const generationModels = useMemo(() => connectedFalGenerationModels(rows(versions.data), projectId), [versions.data, projectId]);
  const fixedModel = generationModels.find(model => model.id === fixedModelId);
  const endpointId = fixedModel?.endpoint ?? "";
  const endpointSchema = useResource<Row>(endpointId ? `/api/eval-endpoints/${endpointId}/schema` : null);
  const gridContract = asRow(endpointSchema.data?.grid);
  const axisFieldSchemas = asRow(gridContract.axis_fields) as Record<string, GridAxisSchema>;
  const axisFields = Object.keys(axisFieldSchemas);
  const defaultAxes = Array.isArray(gridContract.default_axes) ? gridContract.default_axes.map(String).filter(field => axisFields.includes(field)) : [];
  const axisOptions = axisFields.map(field => ({ value: field, label: String(axisFieldSchemas[field]?.label ?? field) }));
  const selectedPromptRows = rows(promptSet.data?.prompts);
  const endpointModels = generationModels.filter(model => model.endpoint === endpointId);

  useEffect(() => {
    if (versions.data == null) return;
    const valid = new Set(generationModels.map(model => model.id));
    setFixedModelId(id => valid.has(id) ? id : "");
    setXModels(ids => ids.filter(id => valid.has(id)));
    setYModels(ids => ids.filter(id => valid.has(id)));
    setZModels(ids => ids.filter(id => valid.has(id)));
  }, [generationModels, versions.data]);
  useEffect(() => {
    if (!endpointId || !axisFields.length || initializedEndpoint.current === endpointId) return;
    initializedEndpoint.current = endpointId;
    const [nextX = "", nextY = "", nextZ = ""] = defaultAxes;
    setXField(nextX);
    setYField(nextY);
    setZField(nextZ);
    setXText(defaultAxisText(axisFieldSchemas[nextX]));
    setYText(defaultAxisText(axisFieldSchemas[nextY]));
    setZText(defaultAxisText(axisFieldSchemas[nextZ]));
    setXModels([]);
    setYModels([]);
    setZModels([]);
    setZEnabled(false);
  }, [endpointId, endpointSchema.data]);
  useEffect(() => {
    if (!promptSetId || promptSets.loading) return;
    if (!rows(promptSets.data).some(item => idOf(item) === promptSetId)) setPromptSetId("");
  }, [promptSetId, promptSets.data, promptSets.loading]);

  const usedAxisFields = [xField, yField, ...(zEnabled ? [zField] : [])].filter(Boolean);
  const fixedSchema = useMemo(() => {
    const fixedFields = Array.isArray(gridContract.fixed_fields) ? gridContract.fixed_fields.map(String).filter(field => !usedAxisFields.includes(field)) : [];
    const fieldSchema = asRow(gridContract.fixed_field_schema);
    const defaults = asRow(endpointSchema.data?.defaults);
    return {
      defaults: Object.fromEntries(fixedFields.filter(field => field in defaults || asRow(fieldSchema[field]).default !== undefined).map(field => [field, field in defaults ? defaults[field] : asRow(fieldSchema[field]).default])),
      request_fields: fixedFields,
      fields: Object.fromEntries(fixedFields.map(field => [field, asRow(fieldSchema[field])])),
    } as Row;
  }, [endpointSchema.data, xField, yField, zField, zEnabled]);
  useEffect(() => {
    setFixedFormValid(fixedFormRef.current?.checkValidity() ?? false);
  }, [fixedSchema, fixedRevision]);

  const fixedValues = parametersFromForm(fixedFormRef.current ? new FormData(fixedFormRef.current) : new FormData(), fixedSchema);
  const fixedSeed = finiteNumber(fixedValues.seed);
  const fixedLoraScale = finiteNumber(fixedValues.lora_scale);
  const fixedExtra = Object.fromEntries(Object.entries(fixedValues).filter(([field]) => !["seed", "lora_scale", "sync_mode"].includes(field)));
  const savedParameters = {
    ...fixedExtra,
    ...(fixedSeed === undefined ? {} : { seed: fixedSeed }),
    ...(fixedLoraScale === undefined ? {} : { lora_scale: fixedLoraScale }),
  };
  const promptIsAxis = usedAxisFields.includes("prompt");
  const effectiveFixedPrompt = promptIsAxis ? undefined : promptSetId ? str(selectedPromptRows[0]?.text, "") : fixedPrompt;

  let x: GridAxis = { field: xField, values: [] };
  let y: GridAxis = { field: yField, values: [] };
  let z: GridAxis | undefined;
  let preview: GridCellPlan[] = [];
  let previewError = "";
  try {
    x = gridAxis(xField, xText, xModels, endpointModels, axisFieldSchemas[xField], promptSetId ? selectedPromptRows : []);
    y = gridAxis(yField, yText, yModels, endpointModels, axisFieldSchemas[yField], promptSetId ? selectedPromptRows : []);
    z = zEnabled ? gridAxis(zField, zText, zModels, endpointModels, axisFieldSchemas[zField], promptSetId ? selectedPromptRows : []) : undefined;
    preview = compileGridPlan({
      x,
      y,
      z,
      availableModels: endpointModels,
      defaultModel: fixedModel,
      fixedPrompt: effectiveFixedPrompt,
      base: { numImages: 1, seed: fixedSeed, loraScale: fixedLoraScale, extra: fixedExtra },
    });
  } catch (reason) {
    previewError = reason instanceof Error ? reason.message : String(reason);
  }
  const duplicateAxis = new Set(usedAxisFields).size !== usedAxisFields.length;
  const gridPreflightError =
    !projectId ? "Select a project." :
      versions.loading ? "Loading registered checkpoints." :
        !fixedModel ? "Select a fixed model / checkpoint to establish the endpoint schema." :
          !endpointId ? "The selected checkpoint has no registered endpoint." :
            endpointSchema.error ? endpointSchema.error :
              endpointSchema.loading || !endpointSchema.data ? "Loading the selected endpoint schema." :
                axisFields.length < 2 ? `Endpoint ${endpointId} does not define enough Grid axes.` :
                  !xField || !yField || zEnabled && !zField ? "Choose every enabled axis." :
                    usedAxisFields.some(field => !axisFields.includes(field)) ? `The selected endpoint does not support ${usedAxisFields.find(field => !axisFields.includes(field))}.` :
                      duplicateAxis ? "Each Grid axis must use a different endpoint field." :
                      !promptSetId ? "Select a saved prompt set version." :
                        promptSetId && promptSet.loading ? "Loading the selected prompt set version." :
                          promptSet.error ? promptSet.error :
                            promptSetId && !selectedPromptRows.length ? "The selected prompt set version is empty." :
                              !fixedFormValid ? "Complete the endpoint-defined fixed fields within their allowed bounds." :
                                previewError || !preview.length ? previewError || "Add axis values to compose a grid." :
                                  preview.some(cell => !cell.model.available || cell.model.provider !== "fal" || cell.model.endpoint !== endpointId) ? `Every cell must use a usable ${endpointId} checkpoint.` : "";
  const uniqueCheckpointCount = new Set(preview.map(cell => cell.model.id)).size;
  const planSignature = JSON.stringify({ projectId, endpointId, promptSetId, x, y, z, fixedRevision, fixedPrompt, models: preview.map(cell => cell.model.id) });
  useEffect(() => { attempt.current = null; }, [planSignature]);

  async function generate() {
    if (submitting) return;
    setError("");
    if (gridPreflightError) { setError(gridPreflightError); return; }
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
      if (!attempt.current.gridId) {
        const savedGrid = await api<Row>("/api/grids", jsonBody({
          name: `${str(promptSet.data?.name, "Prompt set")} · ${axisFieldSchemas[x.field]?.label ?? x.field} × ${axisFieldSchemas[y.field]?.label ?? y.field}`,
          model_version_id: fixedModel!.id,
          prompt_set_id: promptSetId,
          endpoint: endpointId,
          x_axis: { field: x.field, values: x.values.map(value => value.value) },
          y_axis: { field: y.field, values: y.values.map(value => value.value) },
          parameters: savedParameters,
        }));
        attempt.current.gridId = idOf(savedGrid);
      }
      const project = rows(projects.data).find(item => idOf(item) === projectId);
      const queued = await api<Row>("/api/generation-queue", jsonBody({
        workflow: "grid", provider: "fal", context: { kind: "project", project_id: projectId }, model_version_ids: uniqueModels,
        compiled_request_id: ids.parent, client_request_id: ids.parent,
        request: {
          workflow: "grid", endpoint_id: endpointId,
          context: { ...context, projectTitle: str(project?.title ?? project?.name, projectId) },
          models: uniqueModels.map(id => generationModels.find(model => model.id === id)),
          prompt_set: promptSetId ? { id: promptSetId, name: str(promptSet.data?.name), version: promptSet.data?.version } : null,
          grid_definition_id: attempt.current.gridId,
          axes: { x, y, z: z ?? null },
          cells: preview.map(cell => ({ id: cell.id, ordinal: cell.ordinal, x_index: cell.xIndex, y_index: cell.yIndex, z_index: cell.zIndex, x: cell.x, y: cell.y, z: cell.z, model_version_id: cell.model.id, parameters: cell.parameters })),
        },
        children: compiled,
      }));
      setQueuedId(idOf(queued));
      attempt.current = null;
      await queue.reload();
      await savedGrids.reload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSubmitting(false);
    }
  }

  const changeField = (setter: (field: GridAxisField) => void, selectedSetter: (ids: string[]) => void, textSetter: (text: string) => void, field: GridAxisField) => {
    setter(field);
    selectedSetter([]);
    textSetter(defaultAxisText(axisFieldSchemas[field]));
  };
  return <Page title="Grid generator" subtitle="Compose deterministic endpoint-valid X/Y columns and rows with an optional Z subgrid.">
    <Notice error={projects.error || versions.error || promptSets.error || queue.error} loading={projects.loading || versions.loading || promptSets.loading} />
    <Panel title="Fixed / global settings">
      <div className="form-grid">
        <Field label="Project"><Dropdown aria-label="Project" value={projectId} placeholder="Select..." options={rows(projects.data).map(project => ({ value: idOf(project), label: str(project.title ?? project.name) }))} onChange={value => { setProjectId(value); setFixedModelId(""); setPromptSetId(""); initializedEndpoint.current = ""; }} /></Field>
        <ModelCheckpointPicker mode="fixed" models={generationModels} selected={fixedModelId ? [fixedModelId] : []} onChange={ids => { setFixedModelId(ids[0] ?? ""); initializedEndpoint.current = ""; }} label="Fixed model / checkpoint" />
        <Field label="Prompt set version">
          <Dropdown aria-label="Prompt set version" value={promptSetId} disabled={!projectId} options={[{ value: "", label: "Select saved version…" }, ...rows(promptSets.data).map(item => ({ value: idOf(item), label: `${str(item.name)} · v${str(item.version, "1")} · ${str(item.prompt_count, "0")} prompts` }))]} onChange={setPromptSetId} />
        </Field>
        <Field label="Fixed prompt"><textarea value={effectiveFixedPrompt ?? ""} disabled={promptIsAxis || Boolean(promptSetId)} onChange={event => setFixedPrompt(event.target.value)} /></Field>
      </div>
      {fixedModel && <p className="vela-meta">Endpoint schema: {endpointId}</p>}
      <Notice error={endpointSchema.error || promptSet.error} loading={endpointSchema.loading || promptSet.loading} />
      <form ref={fixedFormRef} className="grid-fixed-fields" onInput={() => setFixedRevision(value => value + 1)} onChange={() => setFixedRevision(value => value + 1)}>
        <div className="form-grid"><EndpointFields key={`${endpointId}:${usedAxisFields.join(":")}`} schema={fixedSchema} onChange={() => setFixedRevision(value => value + 1)} /></div>
      </form>
    </Panel>
    <div className="grid-axis-layout">
      <AxisEditor title="X axis · columns" field={xField} onField={field => changeField(setXField, setXModels, setXText, field)} text={xText} onText={setXText} selectedModels={xModels} onModels={setXModels} models={endpointModels} options={axisOptions} schema={axisFieldSchemas[xField]} promptSet={promptSetId ? promptSet.data : null} />
      <AxisEditor title="Y axis · rows" field={yField} onField={field => changeField(setYField, setYModels, setYText, field)} text={yText} onText={setYText} selectedModels={yModels} onModels={setYModels} models={endpointModels} options={axisOptions} schema={axisFieldSchemas[yField]} promptSet={promptSetId ? promptSet.data : null} />
    </div>
    <Panel title="Z axis · optional subgrids">
      <label className="grid-z-toggle"><input type="checkbox" checked={zEnabled} disabled={!zField} onChange={event => setZEnabled(event.target.checked)} /> Enable Z axis</label>
      {zEnabled && <AxisEditor title="Z axis · subgrids" field={zField} onField={field => changeField(setZField, setZModels, setZText, field)} text={zText} onText={setZText} selectedModels={zModels} onModels={setZModels} models={endpointModels} options={axisOptions} schema={axisFieldSchemas[zField]} promptSet={promptSetId ? promptSet.data : null} embedded />}
      {!zEnabled && <p className="vela-meta">One X/Y grid is shown. Enable Z to compose one ordered subgrid per endpoint-supported value.</p>}
    </Panel>
    <div className={`vela-notice${gridPreflightError ? " vela-notice-error" : ""}`} role="status"><strong>{gridPreflightError ? "Grid incomplete" : "Grid preflight"}</strong><span>{gridPreflightError || `${endpointId} accepts ${preview.length} cell${preview.length === 1 ? "" : "s"} across ${uniqueCheckpointCount} checkpoint${uniqueCheckpointCount === 1 ? "" : "s"}.`}</span></div>
    <GridPreview cells={preview} x={x} y={y} z={z} incomplete={Boolean(gridPreflightError)} />
    {error && <div className="vela-notice vela-notice-error" role="alert"><strong>Cannot generate grid</strong><span>{error}</span></div>}
    <button className="vela-button vela-button-primary grid-generate" type="button" disabled={submitting || Boolean(gridPreflightError)} onClick={() => void generate()}>{submitting ? <><LoaderCircle className="vela-spin" size={15} /> Preparing grid...</> : <><Grid3X3 size={15} /> Generate grid</>}</button>
    {queuedId && <div className="vela-notice" role="status"><strong>Grid queued</strong><span><a href={`#/grid/${queuedId}`}>Open grid {queuedId}</a></span></div>}
    <GenerationQueue items={rows(queue.data).filter(row => row.workflow === "grid")} error={queue.error} loading={queue.loading} />
    <Panel title="Saved grids"><Notice error={savedGrids.error} loading={savedGrids.loading} empty={!savedGrids.loading && !rows(savedGrids.data).length} emptyText="No saved grids in this project" emptyHint="Queued Grid results and historical definitions appear here." />{!!rows(savedGrids.data).length && <div className="saved-grid-list">{rows(savedGrids.data).map(grid => <a key={idOf(grid)} href={`#/grid/${idOf(grid)}`}><strong>{str(grid.name, "Saved grid")}</strong><span>{str(grid.endpoint_adapter, "Endpoint unavailable")}</span><Status value={grid.status} /></a>)}</div>}</Panel>
  </Page>;
}

function AxisEditor({ title, field, onField, text, onText, selectedModels, onModels, models, options, schema, promptSet, embedded = false }: {
  title: string;
  field: GridAxisField;
  onField: (field: GridAxisField) => void;
  text: string;
  onText: (text: string) => void;
  selectedModels: string[];
  onModels: (ids: string[]) => void;
  models: GenerationModel[];
  options: Array<{ value: string; label: string }>;
  schema?: GridAxisSchema;
  promptSet?: Row | null;
  embedded?: boolean;
}) {
  const bounds = schema?.min !== undefined || schema?.max !== undefined ? ` Bounds: ${schema.min ?? "−∞"} to ${schema.max ?? "∞"}.` : "";
  const choices = schema?.options?.length ? ` Allowed values: ${schema.options.map(String).join(", ")}.` : "";
  const body = <>
    <Field label="Parameter"><Dropdown className="grid-axis-select" aria-label="Parameter" value={field} disabled={!options.length} options={options} onChange={value => onField(value as GridAxisField)} /></Field>
    {modelAxisField(field)
      ? <ModelCheckpointPicker mode="axis" models={models} selected={selectedModels} onChange={onModels} label="Axis model / checkpoints" />
      : field === "prompt" && promptSet
        ? <div className="grid-prompt-source"><strong>{str(promptSet.name, "Prompt set")} · v{str(promptSet.version, "1")}</strong><span>{rows(promptSet.prompts).length} ordered prompts populate this axis.</span></div>
        : <Field label="Values" hint={`One value per line.${choices}${bounds}`}><textarea aria-label={`${title} values`} value={text} onChange={event => onText(event.target.value)} /></Field>}
  </>;
  return embedded ? <div className="grid-axis-editor">{body}</div> : <Panel title={title}>{body}</Panel>;
}


function gridAxis(field: GridAxisField, text: string, selected: string[], models: GenerationModel[], schema?: GridAxisSchema, promptRows: Row[] = []): GridAxis {
  if (!field || !schema) throw new Error("Wait for the selected endpoint axis schema.");
  if (modelAxisField(field)) return { field, values: selected.map(id => { const model = models.find(item => item.id === id); return { id: `model-${id}`, label: model ? `${model.name} · ${model.checkpointLabel}` : id, value: id }; }) };
  if (field === "prompt" && promptRows.length) return { field, values: promptRows.map((prompt, index) => ({ id: idOf(prompt) || `prompt-${index}`, label: str(prompt.text), value: str(prompt.text) })) };
  return { field, values: parseGridAxisValues(text, schema) };
}

function asRow(value: unknown): Row {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Row : {};
}

function finiteNumber(value: unknown): number | undefined {
  const number = Number(value);
  return value === "" || value == null || !Number.isFinite(number) ? undefined : number;
}

function defaultAxisText(schema?: GridAxisSchema): string {
  return (schema?.default_values ?? []).map(value => {
    if (value && typeof value === "object" && "width" in value && "height" in value) return `${value.width}×${value.height}`;
    return String(value);
  }).join("\n");
}

function GridPreview({ cells, x, y, z, incomplete }: { cells: GridCellPlan[]; x: GridAxis; y: GridAxis; z?: GridAxis; incomplete: boolean }) {
  const groups = z ? Array.from(new Set(cells.map(cell => cell.zIndex))) : [0];
  return <Panel title="Composed grid preview"><div className="grid-preview-tools"><span>X · {x.field} · columns</span><span>Y · {y.field} · rows</span><span>{z ? `Z · ${z.field} · ${groups.length} subgrids` : "One X/Y grid"}</span><span>{cells.length} cells</span></div>{incomplete ? <div className="vela-empty" role="status">Complete the fixed settings and axis values to preview composed cells.</div> : <div className="grid-subgrids">{groups.map(zIndex => { const group = cells.filter(cell => cell.zIndex === zIndex); return <section key={zIndex}>{z && <h3>{group[0]?.z?.label}</h3>}<GridBoard x={x.values} y={y.values} cells={group} /></section>; })}</div>}</Panel>;
}

export function GridBoard({ x, y, cells, completed = false }: { x: GridAxisValue[]; y: GridAxisValue[]; cells: Array<GridCellPlan | Row>; completed?: boolean }) {
  const region = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState(1);
  const [fit, setFit] = useState(false);
  const pan = (horizontal: number, vertical: number) => region.current?.scrollBy({ left: horizontal * (region.current?.clientWidth ?? 0) * 0.75, top: vertical * (region.current?.clientHeight ?? 0) * 0.75, behavior: "smooth" });
  const reset = () => {
    setZoom(1);
    setFit(false);
    region.current?.scrollTo({ left: 0, top: 0, behavior: "smooth" });
  };
  return <div className="grid-board-frame">
    <div className="grid-board-tools" role="toolbar" aria-label="Grid view controls">
      <button type="button" className="vela-button" aria-pressed={fit} onClick={() => setFit(value => !value)}>Fit</button>
      <button type="button" className="vela-button" aria-label="Zoom out" disabled={fit || zoom <= 0.5} onClick={() => setZoom(value => Math.max(0.5, value - 0.25))}>−</button>
      <span aria-live="polite">{fit ? "Fit width" : `${Math.round(zoom * 100)}%`}</span>
      <button type="button" className="vela-button" aria-label="Zoom in" disabled={fit || zoom >= 2} onClick={() => setZoom(value => Math.min(2, value + 0.25))}>+</button>
      <button type="button" className="vela-button" onClick={reset}>Reset</button>
      <button type="button" className="vela-button" aria-label="Pan left" onClick={() => pan(-1, 0)}>←</button>
      <button type="button" className="vela-button" aria-label="Pan right" onClick={() => pan(1, 0)}>→</button>
      <button type="button" className="vela-button" aria-label="Pan up" onClick={() => pan(0, -1)}>↑</button>
      <button type="button" className="vela-button" aria-label="Pan down" onClick={() => pan(0, 1)}>↓</button>
    </div>
    <div
      ref={region}
      className={`grid-board-region${completed ? " grid-board-completed" : " grid-board-preview"}`}
      role="region"
      aria-label={`Grid table with ${y.length} rows and ${x.length} columns. Use arrow keys to pan.`}
      tabIndex={0}
      onKeyDown={event => {
        if (event.key === "ArrowLeft") { event.preventDefault(); pan(-1, 0); }
        if (event.key === "ArrowRight") { event.preventDefault(); pan(1, 0); }
        if (event.key === "ArrowUp") { event.preventDefault(); pan(0, -1); }
        if (event.key === "ArrowDown") { event.preventDefault(); pan(0, 1); }
        if (event.key === "Home") { event.preventDefault(); reset(); }
      }}
      style={{ "--grid-zoom": zoom } as CSSProperties}
      data-fit={fit}
    >
      <table className="grid-board">
        <caption>{completed ? "Generated Grid outputs" : "Planned Grid cells"}</caption>
        <thead><tr><th scope="col">Y ↓ / X →</th>{x.map((entry, index) => <th scope="col" key={`x-${index}-${entry.id}`}>{entry.label}</th>)}</tr></thead>
        <tbody>{y.map((entry, yIndex) => <tr key={`y-${yIndex}-${entry.id}`}><th scope="row">{entry.label}</th>{x.map((xEntry, xIndex) => {
          const cell = cells.find(item => Number((item as Row).x_index ?? (item as GridCellPlan).xIndex) === xIndex && Number((item as Row).y_index ?? (item as GridCellPlan).yIndex) === yIndex);
          const row = cell as Row | undefined;
          const assetId = completed ? str(row?.asset_id ?? row?.asset_revision_id, "") : "";
          const plan = completed ? undefined : cell as GridCellPlan | undefined;
          const status = str(row?.status, assetId ? "completed" : "pending");
          return <td key={`cell-${yIndex}-${xIndex}`} data-x-label={xEntry.label} data-y-label={entry.label}><div className="grid-cell">{assetId ? <AssetImage assetRevisionId={assetId} loading="lazy" alt={`${xEntry.label} by ${entry.label}`} /> : completed ? <span className="grid-cell-status">{status === "failed" ? str(row?.error, "Failed") : status}</span> : <span className="grid-cell-plan"><b>{xIndex + 1} × {yIndex + 1}</b><small>{plan?.parameters.prompt || plan?.model.checkpointLabel || "Configured cell"}</small></span>}</div></td>;
        })}</tr>)}</tbody>
      </table>
    </div>
    <p className="vela-meta grid-scan-help">Tab to the named Grid region, then use Arrow keys to scan. Fit and zoom preserve row/column relationships.</p>
  </div>;
}

export function GridScreen({ id }: { id: string }) {
  const queue = useResource<Row>(`/api/generation-queue/${id}/grid-results`, PROGRESS_POLL);
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
  const projectId = str(data?.project_id ?? asRow(request?.context).projectId ?? asRow(request?.context).project_id, "");
  const promptSetId = str(asRow(request?.prompt_set).id, "");
  const completedCount = cells.filter(cell => cell.asset_id || cell.asset_revision_id).length;
  const returnParams = new URLSearchParams();
  if (projectId) returnParams.set("project", projectId);
  if (promptSetId) returnParams.set("prompt_set", promptSetId);
  const returnHref = `#/grids${returnParams.size ? `?${returnParams}` : ""}`;
  return <div className="comparison-viewer vela-route-stage">
    <header className="viewer-topbar"><div><h1>{str(data?.name, "Grid results")}</h1><span>{data ? `${isLegacy ? "Historical · read only" : str(data.status, "Queued")} · ${completedCount} completed of ${cells.length} defined cells` : "Loading Grid results"}</span></div><div className="viewer-actions"><a className="vela-button" href={returnHref}><ArrowLeft size={15} /> Grid generator</a></div></header>
    <main className="vela-main">
      <Notice error={error} loading={queue.loading || Boolean(queue.error) && legacy.loading} empty={!queue.loading && !legacy.loading && !error && !data} emptyText="Grid result unavailable" emptyHint="Return to the project Grid generator to inspect saved definitions and queue history." />
      {data && <>
        {isLegacy && <div className="vela-notice"><strong>Historical grid</strong><span>This saved GridDefinition remains available for review. New runs use the shared Grid generator and queue.</span></div>}
        <Panel title="Grid review"><Status value={isLegacy ? "saved" : str(data.status, "queued")} /><div className="grid-subgrids">{zValues.map((z, zIndex) => <section key={`${z.id}-${zIndex}`}><h3>{z.label}</h3><GridBoard x={x.values} y={y.values} cells={cells.filter(cell => Number(cell.z_index ?? 0) === zIndex)} completed /></section>)}</div><div className="grid-result-links">{cells.filter(cell => cell.asset_id || cell.asset_revision_id).map(cell => { const cellX = asRow(cell.x); const cellY = asRow(cell.y); return <a key={str(cell.id)} className="vela-button" href={galleryOpenHref({ ...cell, project_id: projectId, origin_type: "EVAL" })}><ExternalLink size={15} /> Open {str(cellX.label ?? cell.x_value, "cell")} × {str(cellY.label ?? cell.y_value, "cell")}</a>; })}</div></Panel>
      </>}
    </main>
  </div>;
}

function legacyAxis(value: unknown, fallback: GridAxisField): GridAxis {
  const axis = value && typeof value === "object" ? value as Row : {};
  const field = str(axis.parameter ?? axis.field, fallback) as GridAxisField;
  const values = Array.isArray(axis.values) ? axis.values : [];
  return { field, values: values.map((entry, index) => ({ id: `legacy-${field}-${index}`, label: str(entry), value: typeof entry === "number" ? entry : str(entry) })) };
}
